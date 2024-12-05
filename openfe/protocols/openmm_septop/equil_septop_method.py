# This code is part of OpenFE and is licensed under the MIT license.
# For details, see https://github.com/OpenFreeEnergy/openfe
"""OpenMM Equilibrium SepTop RBFE Protocol --- :mod:`openfe.protocols.openmm_septop.equil_septop_method`
===============================================================================================================

This module implements the necessary methodology tooling to run a
Separated Topologies RBFE calculation using OpenMM tools and one of the
following alchemical sampling methods:

* Hamiltonian Replica Exchange
* Self-adjusted mixture sampling
* Independent window sampling

Current limitations
-------------------

* Only small molecules are allowed to act as alchemical molecules.
  Alchemically changing protein or solvent components would induce
  perturbations which are too large to be handled by this Protocol.


Acknowledgements
----------------

"""
from __future__ import annotations

import pathlib
import logging
import warnings
from collections import defaultdict
import gufe
import openmm
import openmm.unit
from gufe.components import Component
import itertools
import numpy as np
import numpy.typing as npt
from openff.units import unit
from openmmtools import multistate
import mdtraj as md
from typing import Optional, Union
from typing import Any, Iterable
import simtk.unit as omm_units
import uuid

from gufe import (
    settings,
    ChemicalSystem, SmallMoleculeComponent,
    ProteinComponent, SolventComponent
)
from openfe.protocols.openmm_septop.equil_septop_settings import (
    SepTopSettings,
    OpenMMSolvationSettings, AlchemicalSettings, LambdaSettings,
    MDSimulationSettings, MDOutputSettings,
    MultiStateSimulationSettings, OpenMMEngineSettings,
    IntegratorSettings, MultiStateOutputSettings,
    OpenFFPartialChargeSettings,
    SettingsBaseModel, RestraintsSettings,
)
from ..openmm_utils import system_validation, settings_validation
from .base import BaseSepTopSetupUnit, BaseSepTopRunUnit
from openfe.utils import log_system_probe
from openfe.due import due, Doi
from .femto_restraints import (
    select_receptor_idxs,
    check_receptor_idxs,
    create_boresch_restraint,
)
from .femto_utils import assign_force_groups
from openff.units.openmm import to_openmm


due.cite(Doi("10.5281/zenodo.596622"),
         description="OpenMMTools",
         path="openfe.protocols.openmm_septop.equil_septop_method",
         cite_module=True)

due.cite(Doi("10.1371/journal.pcbi.1005659"),
         description="OpenMM",
         path="openfe.protocols.openmm_septop.equil_septop_method",
         cite_module=True)


logger = logging.getLogger(__name__)


def _get_mdtraj_from_openmm(omm_topology, omm_positions):
    """
    Get an mdtraj object from an OpenMM topology and positions
    """
    mdtraj_topology = md.Topology.from_openmm(omm_topology)
    positions_in_mdtraj_format = np.array(
        omm_positions / omm_units.nanometers)

    unit_cell = omm_topology.getPeriodicBoxVectors() / omm_units.nanometers
    print(unit_cell)
    unit_cell_length = np.array([i[inx] for inx, i in enumerate(unit_cell)])
    print(unit_cell_length)
    mdtraj_system = md.Trajectory(positions_in_mdtraj_format,
                                  mdtraj_topology,
                                  unitcell_lengths=unit_cell_length)
    return mdtraj_system


class SepTopProtocolResult(gufe.ProtocolResult):
    """Dict-like container for the output of a SepTopProtocol
    """
    def __init__(self, **data):
        super().__init__(**data)
        # TODO: Detect when we have extensions and stitch these together?
        if any(len(pur_list) > 2 for pur_list
               in itertools.chain(self.data['solvent'].values(), self.data['complex'].values())):
            raise NotImplementedError("Can't stitch together results yet")

    def get_individual_estimates(self) -> dict[str, list[tuple[unit.Quantity, unit.Quantity]]]:
        """
        Get the individual estimate of the free energies.

        Returns
        -------
        dGs : dict[str, list[tuple[unit.Quantity, unit.Quantity]]]
          A dictionary, keyed `solvent` and `complex for each leg
          of the thermodynamic cycle, with lists of tuples containing
          the individual free energy estimates and associated MBAR
          uncertainties for each repeat of that simulation type.
        """
        complex_dGs = []
        solv_dGs = []

        for pus in self.data['complex'].values():
            print(pus)
            complex_dGs.append((
                pus[0].outputs['unit_estimate'],
                pus[0].outputs['unit_estimate_error']
            ))

        for pus in self.data['solvent'].values():
            print(pus)
            solv_dGs.append((
                pus[0].outputs['unit_estimate'],
                pus[0].outputs['unit_estimate_error']
            ))

        return {'solvent': solv_dGs, 'complex': complex_dGs}

    def get_estimate(self):
        """Get the difference in binding free energy estimate for this calculation.

        Returns
        -------
        ddG : unit.Quantity
          The difference in binding free energy.
          This is a Quantity defined with units.
        """
        def _get_average(estimates):
            # Get the unit value of the first value in the estimates
            u = estimates[0][0].u
            # Loop through estimates and get the free energy values
            # in the unit of the first estimate
            ddGs = [i[0].to(u).m for i in estimates]

            return np.average(ddGs) * u

        individual_estimates = self.get_individual_estimates()
        solv_ddG = _get_average(individual_estimates['solvent'])
        complex_ddG = _get_average(individual_estimates['complex'])
        return solv_ddG - complex_ddG

    def get_uncertainty(self):
        """Get the relative free energy error for this calculation.

        Returns
        -------
        err : unit.Quantity
          The standard deviation between estimates of the relative binding free
          energy. This is a Quantity defined with units.
        """
        def _get_stdev(estimates):
            # Get the unit value of the first value in the estimates
            u = estimates[0][0].u
            # Loop through estimates and get the free energy values
            # in the unit of the first estimate
            ddGs = [i[0].to(u).m for i in estimates]

            return np.std(ddGs) * u

        individual_estimates = self.get_individual_estimates()
        solv_err = _get_stdev(individual_estimates['solvent'])
        complex_err = _get_stdev(individual_estimates['complex'])

        # return the combined error
        return np.sqrt(solv_err**2 + complex_err**2)

    def get_forward_and_reverse_energy_analysis(self) -> dict[str, list[Optional[dict[str, Union[npt.NDArray, unit.Quantity]]]]]:
        """
        Get the reverse and forward analysis of the free energies.

        Returns
        -------
        forward_reverse : dict[str, list[Optional[dict[str, Union[npt.NDArray, unit.Quantity]]]]]
            A dictionary, keyed `complex` and `solvent` for each leg of the
            thermodynamic cycle which each contain a list of dictionaries
            containing the forward and reverse analysis of each repeat
            of that simulation type.

            The forward and reverse analysis dictionaries contain:
              - `fractions`: npt.NDArray
                  The fractions of data used for the estimates
              - `forward_DDGs`, `reverse_DDGs`: unit.Quantity
                  The forward and reverse estimates for each fraction of data
              - `forward_dDDGs`, `reverse_dDDGs`: unit.Quantity
                  The forward and reverse estimate uncertainty for each
                  fraction of data.

            If one of the cycle leg list entries is ``None``, this indicates
            that the analysis could not be carried out for that repeat. This
            is most likely caused by MBAR convergence issues when attempting to
            calculate free energies from too few samples.

        Raises
        ------
        UserWarning
          * If any of the forward and reverse dictionaries are ``None`` in a
            given thermodynamic cycle leg.
        """

        forward_reverse: dict[str, list[Optional[dict[str, Union[npt.NDArray, unit.Quantity]]]]] = {}

        for key in ['complex', 'solvent']:
            forward_reverse[key] = [
                pus[0].outputs['forward_and_reverse_energies']
                for pus in self.data[key].values()
            ]

            if None in forward_reverse[key]:
                wmsg = (
                    "One or more ``None`` entries were found in the forward "
                    f"and reverse dictionaries of the repeats of the {key} "
                    "calculations. This is likely caused by an MBAR convergence "
                    "failure caused by too few independent samples when "
                    "calculating the free energies of the 10% timeseries slice."
                )
                warnings.warn(wmsg)

        return forward_reverse

    def get_overlap_matrices(self) -> dict[str, list[dict[str, npt.NDArray]]]:
        """
        Get a the MBAR overlap estimates for all legs of the simulation.

        Returns
        -------
        overlap_stats : dict[str, list[dict[str, npt.NDArray]]]
          A dictionary with keys `complex` and `solvent` for each
          leg of the thermodynamic cycle, which each containing a
          list of dictionaries with the MBAR overlap estimates of
          each repeat of that simulation type.

          The underlying MBAR dictionaries contain the following keys:
            * ``scalar``: One minus the largest nontrivial eigenvalue
            * ``eigenvalues``: The sorted (descending) eigenvalues of the
              overlap matrix
            * ``matrix``: Estimated overlap matrix of observing a sample from
              state i in state j
        """
        # Loop through and get the repeats and get the matrices
        overlap_stats: dict[str, list[dict[str, npt.NDArray]]] = {}

        for key in ['complex', 'solvent']:
            overlap_stats[key] = [
                pus[0].outputs['unit_mbar_overlap']
                for pus in self.data[key].values()
            ]

        return overlap_stats

    def get_replica_transition_statistics(self) -> dict[str, list[dict[str, npt.NDArray]]]:
        """
        Get the replica exchange transition statistics for all
        legs of the simulation.

        Note
        ----
        This is currently only available in cases where a replica exchange
        simulation was run.

        Returns
        -------
        repex_stats : dict[str, list[dict[str, npt.NDArray]]]
          A dictionary with keys `complex` and `solvent` for each
          leg of the thermodynamic cycle, which each containing
          a list of dictionaries containing the replica transition
          statistics for each repeat of that simulation type.

          The replica transition statistics dictionaries contain the following:
            * ``eigenvalues``: The sorted (descending) eigenvalues of the
              lambda state transition matrix
            * ``matrix``: The transition matrix estimate of a replica switching
              from state i to state j.
        """
        repex_stats: dict[str, list[dict[str, npt.NDArray]]] = {}
        try:
            for key in ['complex', 'solvent']:
                repex_stats[key] = [
                    pus[0].outputs['replica_exchange_statistics']
                    for pus in self.data[key].values()
                ]
        except KeyError:
            errmsg = ("Replica exchange statistics were not found, "
                      "did you run a repex calculation?")
            raise ValueError(errmsg)

        return repex_stats

    def get_replica_states(self) -> dict[str, list[npt.NDArray]]:
        """
        Get the timeseries of replica states for all simulation legs.

        Returns
        -------
        replica_states : dict[str, list[npt.NDArray]]
          Dictionary keyed `complex` and `solvent` for each leg of
          the thermodynamic cycle, with lists of replica states
          timeseries for each repeat of that simulation type.
        """
        replica_states: dict[str, list[npt.NDArray]] = {
            'complex': [], 'solvent': []
        }

        def is_file(filename: str):
            p = pathlib.Path(filename)

            if not p.exists():
                errmsg = f"File could not be found {p}"
                raise ValueError(errmsg)

            return p

        def get_replica_state(nc, chk):
            nc = is_file(nc)
            dir_path = nc.parents[0]
            chk = is_file(dir_path / chk).name

            reporter = multistate.MultiStateReporter(
                storage=nc, checkpoint_storage=chk, open_mode='r'
            )

            retval = np.asarray(reporter.read_replica_thermodynamic_states())
            reporter.close()

            return retval

        for key in ['complex', 'solvent']:
            for pus in self.data[key].values():
                states = get_replica_state(
                    pus[0].outputs['nc'],
                    pus[0].outputs['last_checkpoint'],
                )
                replica_states[key].append(states)

        return replica_states

    def equilibration_iterations(self) -> dict[str, list[float]]:
        """
        Get the number of equilibration iterations for each simulation.

        Returns
        -------
        equilibration_lengths : dict[str, list[float]]
          Dictionary keyed `complex` and `solvent` for each leg
          of the thermodynamic cycle, with lists containing the
          number of equilibration iterations for each repeat
          of that simulation type.
        """
        equilibration_lengths: dict[str, list[float]] = {}

        for key in ['complex', 'solvent']:
            equilibration_lengths[key] = [
                pus[0].outputs['equilibration_iterations']
                for pus in self.data[key].values()
            ]

        return equilibration_lengths

    def production_iterations(self) -> dict[str, list[float]]:
        """
        Get the number of production iterations for each simulation.
        Returns the number of uncorrelated production samples for each
        repeat of the calculation.

        Returns
        -------
        production_lengths : dict[str, list[float]]
          Dictionary keyed `complex` and `solvent` for each leg of the
          thermodynamic cycle, with lists with the number
          of production iterations for each repeat of that simulation
          type.
        """
        production_lengths: dict[str, list[float]] = {}

        for key in ['complex', 'solvent']:
            production_lengths[key] = [
                pus[0].outputs['production_iterations']
                for pus in self.data[key].values()
            ]

        return production_lengths


class SepTopProtocol(gufe.Protocol):
    """
    SepTop RBFE calculations using OpenMM and OpenMMTools.

    See Also
    --------
    :mod:`openfe.protocols`
    :class:`openfe.protocols.openmm_septop.SepTopSettings`
    :class:`openfe.protocols.openmm_septop.SepTopProtocolResult`
    :class:`openfe.protocols.openmm_septop.SepTopComplexUnit`
    :class:`openfe.protocols.openmm_septop.SepTopSolventUnit`
    """
    result_cls = SepTopProtocolResult
    _settings: SepTopSettings

    @classmethod
    def _default_settings(cls):
        """A dictionary of initial settings for this creating this Protocol

        These settings are intended as a suitable starting point for creating
        an instance of this protocol.  It is recommended, however that care is
        taken to inspect and customize these before performing a Protocol.

        Returns
        -------
        Settings
          a set of default settings
        """
        return SepTopSettings(
            protocol_repeats=1,
            solvent_forcefield_settings=settings.OpenMMSystemGeneratorFFSettings(),
            complex_forcefield_settings=settings.OpenMMSystemGeneratorFFSettings(),
            thermo_settings=settings.ThermoSettings(
                temperature=298.15 * unit.kelvin,
                pressure=1 * unit.bar,
            ),
            alchemical_settings=AlchemicalSettings(),
            lambda_settings=LambdaSettings(
            ),
            partial_charge_settings=OpenFFPartialChargeSettings(),
            solvent_solvation_settings=OpenMMSolvationSettings(
                solvent_padding=1.8 * unit.nanometer
            ),
            complex_solvation_settings=OpenMMSolvationSettings(),
            complex_engine_settings=OpenMMEngineSettings(),
            solvent_engine_settings=OpenMMEngineSettings(),
            integrator_settings=IntegratorSettings(),
            solvent_equil_simulation_settings=MDSimulationSettings(
                equilibration_length_nvt=0.1 * unit.nanosecond,
                equilibration_length=0.2 * unit.nanosecond,
                production_length=0.5 * unit.nanosecond,
            ),
            solvent_equil_output_settings=MDOutputSettings(
                equil_nvt_structure='equil_nvt_structure.pdb',
                equil_npt_structure='equil_npt_structure.pdb',
                production_trajectory_filename='production_equil.xtc',
                log_output='equil_simulation.log',
            ),
            solvent_simulation_settings=MultiStateSimulationSettings(
                n_replicas=19,
                minimization_steps=10000,
                equilibration_length=1.0 * unit.nanosecond,
                production_length=10.0 * unit.nanosecond,
            ),
            solvent_output_settings=MultiStateOutputSettings(
                output_filename='solvent.nc',
                checkpoint_storage_filename='solvent_checkpoint.nc',
            ),
            complex_equil_simulation_settings=MDSimulationSettings(
                equilibration_length_nvt=None,
                equilibration_length=0.2 * unit.nanosecond,
                production_length=0.5 * unit.nanosecond,
            ),
            complex_equil_output_settings=MDOutputSettings(
                equil_nvt_structure=None,
                equil_npt_structure='equil_structure.pdb',
                production_trajectory_filename='production_equil.xtc',
                log_output='equil_simulation.log',
            ),
            complex_simulation_settings=MultiStateSimulationSettings(
                n_replicas=19,
                equilibration_length=0.5 * unit.nanosecond,
                production_length=2.0 * unit.nanosecond,
            ),
            complex_output_settings=MultiStateOutputSettings(
                output_filename='complex.nc',
                checkpoint_storage_filename='complex_checkpoint.nc'
            ),
            solvent_restraints_settings=RestraintsSettings(
                k_distance=1000 * unit.kilojoule_per_mole / unit.nanometer**2,
                k_theta=None,
            ),
            complex_restraints_settings=RestraintsSettings(
                k_distance=8368.0 * unit.kilojoule_per_mole / unit.nanometer**2
            ),
        )

    @staticmethod
    def _validate_complex_endstates(
        stateA: ChemicalSystem, stateB: ChemicalSystem,
    ) -> None:
        """
        A complex transformation is defined (in terms of gufe components)
        as starting from one or more ligands and a protein in solvent and
        ending up in a state with one less ligand.

        Parameters
        ----------
        stateA : ChemicalSystem
          The chemical system of end state A
        stateB : ChemicalSystem
          The chemical system of end state B

        Raises
        ------
        ValueError
          If there is no SolventComponent and no ProteinComponent
          in either stateA or stateB.
        """
        # check that there is a protein component
        if not any(
            isinstance(comp, ProteinComponent) for comp in stateA.values()
        ):
            errmsg = "No ProteinComponent found in stateA"
            raise ValueError(errmsg)

        if not any(
            isinstance(comp, ProteinComponent) for comp in stateB.values()
        ):
            errmsg = "No ProteinComponent found in stateB"
            raise ValueError(errmsg)

        # check that there is a solvent component
        if not any(
            isinstance(comp, SolventComponent) for comp in stateA.values()
        ):
            errmsg = "No SolventComponent found in stateA"
            raise ValueError(errmsg)

        if not any(
            isinstance(comp, SolventComponent) for comp in stateB.values()
        ):
            errmsg = "No SolventComponent found in stateB"
            raise ValueError(errmsg)

    @staticmethod
    def _validate_alchemical_components(
        alchemical_components: dict[str, list[Component]]
    ) -> None:
        """
        Checks that the ChemicalSystem alchemical components are correct.

        Parameters
        ----------
        alchemical_components : Dict[str, list[Component]]
          Dictionary containing the alchemical components for
          stateA and stateB.

        Raises
        ------
        ValueError
          * If there are no or more than one alchemical components in state A.
          * If there are no or more than one alchemical components in state B.
          * If there are any alchemical components that are not
            SmallMoleculeComponents

        Notes
        -----
        * Currently doesn't support alchemical components which are not
          SmallMoleculeComponents.
        * Currently doesn't support more than one alchemical component
          being desolvated.
        """

        # Crash out if there are less or more than one alchemical components
        # in state A and B
        if len(alchemical_components['stateA']) != 1:
            errmsg = ("Exactly one alchemical components must be present in stateA. "
                      f"Found {len(alchemical_components['stateA'])} "
                      "alchemical components in stateA")
            raise ValueError(errmsg)

        if len(alchemical_components['stateB']) != 1:
            errmsg = ("Exactly one alchemical components must be present in stateB. "
                      f"Found {len(alchemical_components['stateB'])} "
                      "alchemical components in stateB")
            raise ValueError(errmsg)

        # Crash out if any of the alchemical components are not
        # SmallMoleculeComponent
        alchem_components_states = [alchemical_components['stateA'], alchemical_components['stateB']]
        for state in alchem_components_states:
            for comp in state:
                if not isinstance(comp, SmallMoleculeComponent):
                    errmsg = ("Non SmallMoleculeComponent alchemical species "
                              "are not currently supported")
                    raise ValueError(errmsg)

    @staticmethod
    def _validate_lambda_schedule(
            lambda_settings: LambdaSettings,
            simulation_settings: MultiStateSimulationSettings,
    ) -> None:
        """
        Checks that the lambda schedule is set up correctly.

        Parameters
        ----------
        lambda_settings : LambdaSettings
          the lambda schedule Settings
        simulation_settings : MultiStateSimulationSettings
          the settings for either the complex or solvent phase

        Raises
        ------
        ValueError
          If the number of lambda windows differs for electrostatics and sterics.
          If the number of replicas does not match the number of lambda windows.
        Warnings
          If there are non-zero values for restraints (lambda_restraints).
        """

        lambda_elec_ligandA = lambda_settings.lambda_elec_ligandA
        lambda_elec_ligandB = lambda_settings.lambda_elec_ligandB
        lambda_vdw_ligandA = lambda_settings.lambda_vdw_ligandA
        lambda_vdw_ligandB = lambda_settings.lambda_vdw_ligandB
        lambda_restraints_ligandA = lambda_settings.lambda_restraints_ligandA
        lambda_restraints_ligandB = lambda_settings.lambda_restraints_ligandB
        n_replicas = simulation_settings.n_replicas

        # Ensure that all lambda components have equal amount of windows
        lambda_components = [lambda_vdw_ligandA, lambda_vdw_ligandB,
                             lambda_elec_ligandA, lambda_elec_ligandB,
                             lambda_restraints_ligandA, lambda_restraints_ligandB]
        it = iter(lambda_components)
        the_len = len(next(it))
        if not all(len(l) == the_len for l in it):
            errmsg = (
                "Components elec, vdw, and restraints must have equal amount"
                f" of lambda windows. Got {len(lambda_elec_ligandA)} and "
                f"{len(lambda_elec_ligandB)} elec lambda windows, "
                f"{len(lambda_vdw_ligandA)} and {len(lambda_vdw_ligandB)} vdw "
                f"lambda windows, and {len(lambda_restraints_ligandA)} and "
                f"{len(lambda_restraints_ligandB)} restraints lambda windows.")
            raise ValueError(errmsg)

        # Ensure that number of overall lambda windows matches number of lambda
        # windows for individual components
        if n_replicas != len(lambda_vdw_ligandB):
            errmsg = (f"Number of replicas {n_replicas} does not equal the"
                      f" number of lambda windows {len(lambda_vdw_ligandB)}")
            raise ValueError(errmsg)

        # # Check if there are lambda windows with naked charges
        # # Leaving this out for now till I've figured out how the lambda
        # # scheduling works
        # for inx, lam in enumerate(lambda_elec):
        #     if lam < 1 and lambda_vdw[inx] == 1:
        #         errmsg = (
        #             "There are states along this lambda schedule "
        #             "where there are atoms with charges but no LJ "
        #             f"interactions: lambda {inx}: "
        #             f"elec {lam} vdW {lambda_vdw[inx]}")
        #         raise ValueError(errmsg)

    def _create(
        self,
        stateA: ChemicalSystem,
        stateB: ChemicalSystem,
        mapping: Optional[Union[gufe.ComponentMapping, list[gufe.ComponentMapping]]] = None,
        extends: Optional[gufe.ProtocolDAGResult] = None,
    ) -> list[gufe.ProtocolUnit]:
        # TODO: extensions
        if extends:  # pragma: no-cover
            raise NotImplementedError("Can't extend simulations yet")

        # Validate components and get alchemical components
        self._validate_complex_endstates(stateA, stateB)
        alchem_comps = system_validation.get_alchemical_components(
            stateA, stateB,
        )
        self._validate_alchemical_components(alchem_comps)

        # Validate the lambda schedule
        self._validate_lambda_schedule(self.settings.lambda_settings,
                                       self.settings.solvent_simulation_settings)
        self._validate_lambda_schedule(self.settings.lambda_settings,
                                       self.settings.complex_simulation_settings)

        # Check solvent compatibility
        solv_nonbonded_method = self.settings.solvent_forcefield_settings.nonbonded_method
        # Use the more complete system validation solvent checks
        system_validation.validate_solvent(stateA, solv_nonbonded_method)

        # Validate protein component
        system_validation.validate_protein(stateA)

        # Get the name of the alchemical species
        alchname_A = alchem_comps['stateA'][0].name
        alchname_B = alchem_comps['stateB'][0].name

        # Create list units for complex and solvent transforms

        solvent_setup = [
            SepTopSolventSetupUnit(
                protocol=self,
                stateA=stateA,
                stateB=stateB,
                alchemical_components=alchem_comps,
                generation=0, repeat_id=int(uuid.uuid4()),
                name=(f"SepTop RBFE Setup, transformation {alchname_A} to "
                      f"{alchname_B}, solvent leg: repeat {i} generation 0"),
            )
            for i in range(self.settings.protocol_repeats)
        ]

        solvent_run = [
            SepTopSolventRunUnit(
                protocol=self,
                stateA=stateA,
                stateB=stateB,
                alchemical_components=alchem_comps,
                setup=solvent_setup[i],
                generation=0, repeat_id=int(uuid.uuid4()),
                name=(f"SepTop RBFE Run, transformation {alchname_A} to "
                      f"{alchname_B}, solvent leg: repeat {i} generation 0"),
            )
            for i in range(self.settings.protocol_repeats)
        ]

        complex_setup = [
            SepTopComplexSetupUnit(
                protocol=self,
                stateA=stateA,
                stateB=stateB,
                alchemical_components=alchem_comps,
                generation=0, repeat_id=int(uuid.uuid4()),
                name=(f"SepTop RBFE Setup, transformation {alchname_A} to "
                      f"{alchname_B}, complex leg: repeat {i} generation 0"),
            )
            for i in range(self.settings.protocol_repeats)
        ]

        complex_run = [
            SepTopComplexRunUnit(
                protocol=self,
                stateA=stateA,
                stateB=stateB,
                alchemical_components=alchem_comps,
                setup=complex_setup[i],
                generation=0, repeat_id=int(uuid.uuid4()),
                name=(f"SepTop RBFE Run, transformation {alchname_A} to "
                      f"{alchname_B}, complex leg: repeat {i} generation 0"),
            )
            for i in range(self.settings.protocol_repeats)
        ]

        return solvent_setup + solvent_run + complex_setup + complex_run

    def _gather(
        self, protocol_dag_results: Iterable[gufe.ProtocolDAGResult]
    ) -> dict[str, dict[str, Any]]:
        # result units will have a repeat_id and generation
        # first group according to repeat_id
        print("Gathering results test1")
        unsorted_solvent_repeats_setup = defaultdict(list)
        unsorted_solvent_repeats_run = defaultdict(list)
        unsorted_complex_repeats_setup = defaultdict(list)
        unsorted_complex_repeats_run = defaultdict(list)
        for d in protocol_dag_results:
            print(d)
            pu: gufe.ProtocolUnitResult
            for pu in d.protocol_unit_results:
                print(pu)
                if not pu.ok():
                    print('PU not ok')
                    continue
                if pu.outputs['simtype'] == 'solvent':
                    print('Getting solvent pus')
                    if 'Run' in pu.name:
                        print('Run')
                        unsorted_solvent_repeats_run[
                            pu.outputs['repeat_id']].append(pu)
                    elif 'Setup' in pu.name:
                        print('Setup')
                        unsorted_solvent_repeats_setup[
                            pu.outputs['repeat_id']].append(pu)
                else:
                    print('Getting complex pus')
                    if 'Run' in pu.name:
                        print('Run')
                        unsorted_complex_repeats_run[
                            pu.outputs['repeat_id']].append(pu)
                    elif 'Setup' in pu.name:
                        print('Setup')
                        unsorted_complex_repeats_setup[
                            pu.outputs['repeat_id']].append(pu)

        repeats: dict[str, dict[str, list[gufe.ProtocolUnitResult]]] = {
            'solvent_setup': {}, 'solvent': {},
            'complex_setup': {}, 'complex': {},
        }
        for k, v in unsorted_solvent_repeats_setup.items():
            repeats['solvent_setup'][str(k)] = sorted(v, key=lambda x: x.outputs['generation'])
        for k, v in unsorted_solvent_repeats_run.items():
            repeats['solvent'][str(k)] = sorted(v, key=lambda x: x.outputs['generation'])

        for k, v in unsorted_complex_repeats_setup.items():
            repeats['complex_setup'][str(k)] = sorted(v, key=lambda x: x.outputs['generation'])
        for k, v in unsorted_complex_repeats_run.items():
            repeats['complex'][str(k)] = sorted(v, key=lambda x: x.outputs['generation'])
        print(repeats)
        return repeats


class SepTopComplexSetupUnit(BaseSepTopSetupUnit):
    """
    Protocol Unit for the complex phase of a SepTop free energy calculation
    """
    def _get_components(self):
        """
        Get the relevant components for a complex transformation.

        Returns
        -------
        alchem_comps : dict[str, Component]
          A list of alchemical components
        solv_comp : SolventComponent
          The SolventComponent of the system
        prot_comp : Optional[ProteinComponent]
          The protein component of the system, if it exists.
        small_mols : dict[SmallMoleculeComponent: OFFMolecule]
          SmallMoleculeComponents to add to the system.
        """
        stateA = self._inputs['stateA']
        alchem_comps = self._inputs['alchemical_components']

        solv_comp, prot_comp, small_mols = system_validation.get_components(stateA)
        small_mols = {m: m.to_openff() for m in small_mols}
        # Also get alchemical smc from state B
        small_mols_B = {m: m.to_openff()
                        for m in alchem_comps['stateB']}
        small_mols = small_mols | small_mols_B

        return alchem_comps, solv_comp, prot_comp, small_mols

    def _handle_settings(self) -> dict[str, SettingsBaseModel]:
        """
        Extract the relevant settings for a complex transformation.

        Returns
        -------
        settings : dict[str, SettingsBaseModel]
          A dictionary with the following entries:
            * forcefield_settings : OpenMMSystemGeneratorFFSettings
            * thermo_settings : ThermoSettings
            * charge_settings : OpenFFPartialChargeSettings
            * solvation_settings : OpenMMSolvationSettings
            * alchemical_settings : AlchemicalSettings
            * lambda_settings : LambdaSettings
            * engine_settings : OpenMMEngineSettings
            * integrator_settings : IntegratorSettings
            * equil_simulation_settings : MDSimulationSettings
            * equil_output_settings : MDOutputSettings
            * simulation_settings : SimulationSettings
            * output_settings: MultiStateOutputSettings
            * restraint_settings: RestraintsSettings
        """
        prot_settings = self._inputs['protocol'].settings

        settings = {
            'forcefield_settings': prot_settings.complex_forcefield_settings,
            'thermo_settings': prot_settings.thermo_settings,
            'charge_settings': prot_settings.partial_charge_settings,
            'solvation_settings': prot_settings.complex_solvation_settings,
            'alchemical_settings': prot_settings.alchemical_settings,
            'lambda_settings': prot_settings.lambda_settings,
            'engine_settings': prot_settings.complex_engine_settings,
            'integrator_settings': prot_settings.integrator_settings,
            'equil_simulation_settings':
                prot_settings.complex_equil_simulation_settings,
            'equil_output_settings':
                prot_settings.complex_equil_output_settings,
            'simulation_settings': prot_settings.complex_simulation_settings,
            'output_settings': prot_settings.complex_output_settings,
            'restraint_settings': prot_settings.complex_restraints_settings}

        settings_validation.validate_timestep(
            settings['forcefield_settings'].hydrogen_mass,
            settings['integrator_settings'].timestep
        )

        return settings

    @staticmethod
    def _update_positions(
            omm_topology_A, omm_topology_B, positions_A, positions_B,
            atom_indices_A, atom_indices_B,
    ) -> npt.NDArray:
        mdtraj_complex_A = _get_mdtraj_from_openmm(omm_topology_A, positions_A)
        mdtraj_complex_B = _get_mdtraj_from_openmm(omm_topology_B, positions_B)
        mdtraj_complex_B.superpose(mdtraj_complex_A,
                                   atom_indices=mdtraj_complex_A.topology.select(
                                       'backbone'))
        # Extract updated system positions.
        updated_positions_B = mdtraj_complex_B.openmm_positions(0)

        return updated_positions_B

    @staticmethod
    def _add_restraints(
            system: openmm.System,
            positions: np.array,
            topology: openmm.Topology,
            ligand_1,
            ligand_2,
            settings,
            ligand_1_ref_idxs: tuple[int, int, int],
            ligand_2_ref_idxs: tuple[int, int, int],
            ligand_1_idxs: tuple[int, int, int],
            ligand_2_idxs: tuple[int, int, int],
    ) -> openmm.System:

        # Get mdtraj object for system
        traj = _get_mdtraj_from_openmm(topology, positions)
        # Get mdtraj object for ligands
        ligand_1_mdtraj = md.Trajectory(
            np.array(ligand_1.get_positions() / omm_units.nanometers),
            md.Topology.from_openmm(ligand_1.to_openmm()))
        ligand_2_mdtraj = md.Trajectory(
            np.array(ligand_2.get_positions() / omm_units.nanometers),
            md.Topology.from_openmm(ligand_2.to_openmm()))

        # Select the reference indices in the receptor
        receptor_ref_idxs_1 = select_receptor_idxs(
            traj, ligand_1_mdtraj, ligand_1_ref_idxs
        )
        print(receptor_ref_idxs_1)
        receptor_ref_idxs_2 = receptor_ref_idxs_1

        if not check_receptor_idxs(
                traj, receptor_ref_idxs_1, ligand_2_mdtraj, ligand_2_ref_idxs
        ):
            receptor_ref_idxs_2 = select_receptor_idxs(
                traj, ligand_2_mdtraj, ligand_2_ref_idxs)
            print(receptor_ref_idxs_2)

        # Convert restraint units to openmm
        k_distance = to_openmm(settings["restraint_settings"].k_distance)
        print(k_distance)
        k_theta = to_openmm(settings["restraint_settings"].k_theta)
        print(k_theta)

        force_A = create_boresch_restraint(
            receptor_ref_idxs_1[::-1],  # expects [r3, r2, r1], not [r1, r2, r3]
            ligand_1_idxs,
            positions,
            k_distance,
            k_theta,
            "lambda_restraints_ligandA",
        )
        system.addForce(force_A)
        force_B = create_boresch_restraint(
            receptor_ref_idxs_2[::-1],
            # expects [r3, r2, r1], not [r1, r2, r3]
            ligand_2_idxs,
            positions,
            k_distance,
            k_theta,
            "lambda_restraints_ligandB",
        )
        system.addForce(force_B)

        assign_force_groups(system)

        return system

    def _execute(
        self, ctx: gufe.Context, **kwargs,
    ) -> dict[str, Any]:
        log_system_probe(logging.INFO, paths=[ctx.scratch])

        outputs = self.run(scratch_basepath=ctx.scratch,
                           shared_basepath=ctx.shared)

        return {
            'repeat_id': self._inputs['repeat_id'],
            'generation': self._inputs['generation'],
            'simtype': 'complex',
            **outputs
        }


class SepTopSolventSetupUnit(BaseSepTopSetupUnit):
    """
    Protocol Unit for the solvent phase of an relative SepTop free energy
    """

    def _get_components(self):
        """
        Get the relevant components for a solvent transformation.

        Note
        -----
        The solvent portion of the transformation is the transformation of one
        ligand into the other in the solvent. The only thing that
        should be present is the alchemical species in state A and state B
        and the SolventComponent.

        Returns
        -------
        alchem_comps : dict[str, Component]
          A list of alchemical components
        solv_comp : SolventComponent
          The SolventComponent of the system
        prot_comp : Optional[ProteinComponent]
          The protein component of the system, if it exists.
        small_mols : dict[SmallMoleculeComponent: OFFMolecule]
          SmallMoleculeComponents to add to the system.
        """
        stateA = self._inputs['stateA']
        alchem_comps = self._inputs['alchemical_components']

        small_mols_A = {m: m.to_openff()
                        for m in alchem_comps['stateA']}
        small_mols_B = {m: m.to_openff()
                        for m in alchem_comps['stateB']}
        small_mols = small_mols_A | small_mols_B

        solv_comp, _, _ = system_validation.get_components(stateA)

        # 1. We don't need to check that solv_comp is not None, otherwise
        # an error will have been raised when calling `validate_solvent`
        # in the Protocol's `_create`.
        # 2. ProteinComps can't be alchem_comps (for now), so will
        # be returned as None
        return alchem_comps, solv_comp, None, small_mols

    def _handle_settings(self) -> dict[str, SettingsBaseModel]:
        """
        Extract the relevant settings for a complex transformation.

        Returns
        -------
        settings : dict[str, SettingsBaseModel]
          A dictionary with the following entries:
            * forcefield_settings : OpenMMSystemGeneratorFFSettings
            * thermo_settings : ThermoSettings
            * charge_settings : OpenFFPartialChargeSettings
            * solvation_settings : OpenMMSolvationSettings
            * alchemical_settings : AlchemicalSettings
            * lambda_settings : LambdaSettings
            * engine_settings : OpenMMEngineSettings
            * integrator_settings : IntegratorSettings
            * equil_simulation_settings : MDSimulationSettings
            * equil_output_settings : MDOutputSettings
            * simulation_settings : MultiStateSimulationSettings
            * output_settings: MultiStateOutputSettings
            * restraint_settings: RestraintsSettings
        """
        prot_settings = self._inputs['protocol'].settings

        settings = {}
        settings['forcefield_settings'] = prot_settings.solvent_forcefield_settings
        settings['thermo_settings'] = prot_settings.thermo_settings
        settings['charge_settings'] = prot_settings.partial_charge_settings
        settings['solvation_settings'] = prot_settings.solvent_solvation_settings
        settings['alchemical_settings'] = prot_settings.alchemical_settings
        settings['lambda_settings'] = prot_settings.lambda_settings
        settings['engine_settings'] = prot_settings.solvent_engine_settings
        settings['integrator_settings'] = prot_settings.integrator_settings
        settings['equil_simulation_settings'] = prot_settings.solvent_equil_simulation_settings
        settings['equil_output_settings'] = prot_settings.solvent_equil_output_settings
        settings['simulation_settings'] = prot_settings.solvent_simulation_settings
        settings['output_settings'] = prot_settings.solvent_output_settings
        settings['restraint_settings'] = prot_settings.solvent_restraints_settings

        settings_validation.validate_timestep(
            settings['forcefield_settings'].hydrogen_mass,
            settings['integrator_settings'].timestep
        )

        return settings


    @staticmethod
    def _update_positions(
            omm_topology_A, omm_topology_B, positions_A, positions_B,
            atom_indices_A, atom_indices_B,
    ) -> npt.NDArray:

        # Offset ligand B from ligand A in the solvent
        equ_pos_ligandA = positions_A[
                          atom_indices_A[0]:atom_indices_A[-1] + 1]
        equ_pos_ligandB = positions_B[
                          atom_indices_B[0]:atom_indices_B[-1] + 1]

        # Get the mdtraj system of ligand B and the unit cell
        unit_cell = omm_topology_A.getPeriodicBoxVectors()
        unit_cell = [i[inx] for inx, i in enumerate(unit_cell)]
        mdtraj_system_B = _get_mdtraj_from_openmm(omm_topology_B,
                                                  positions_B)

        ligand_1_radius = np.linalg.norm(
            equ_pos_ligandA - equ_pos_ligandA.mean(axis=0), axis=1).max()
        ligand_2_radius = np.linalg.norm(
            equ_pos_ligandB - equ_pos_ligandB.mean(axis=0), axis=1).max()
        ligand_distance = (ligand_1_radius + ligand_2_radius) * 1.5 * omm_units.nanometer
        if ligand_distance > min(unit_cell) / 2:
            ligand_distance = min(unit_cell) / 2

        ligand_offset = equ_pos_ligandA.mean(0) - equ_pos_ligandB.mean(0)
        ligand_offset[0] += ligand_distance
        print(ligand_offset)

        # Offset the ligandB.
        mdtraj_system_B.xyz[0][atom_indices_B,
        :] += ligand_offset / omm_units.nanometers

        # Extract updated system positions.
        updated_positions_B = mdtraj_system_B.openmm_positions(0)

        return updated_positions_B

    @staticmethod
    def _add_restraints(
        system: openmm.System,
        positions,
        topology,
        ligand_1,
        ligand_2,
        settings,
        ligand_1_ref_idxs: list[int],
        ligand_2_ref_idxs: list[int],
        ligand_1_idxs: tuple[int, int, int],
        ligand_2_idxs: tuple[int, int, int],
    ) -> openmm.System:
        """Apply a distance restraints between the ligands.

        Args:
            system: The OpenMM system to add the restraints to.
            topology: The full topology of the complex phase.
            ligand_1_ref_idx: The reference index of the first ligand.
            ligand_2_ref_idx: The reference index of the second ligand.
        """

        coords = positions
        # Taking the middle reference atom
        distance = np.linalg.norm(
            coords[ligand_1_idxs[1]] - coords[ligand_2_idxs[1]])
        print(distance)

        k_distance = to_openmm(settings['restraint_settings'].k_distance)

        force = openmm.HarmonicBondForce()
        force.addBond(
            ligand_1_idxs[1],
            ligand_2_idxs[1],
            distance * openmm.unit.nanometers,
            k_distance,
        )
        force.setName("alignment_restraint")
        force.setForceGroup(6)

        system.addForce(force)

        return system


    def _execute(
        self, ctx: gufe.Context, **kwargs,
    ) -> dict[str, Any]:
        log_system_probe(logging.INFO, paths=[ctx.scratch])

        outputs = self.run(scratch_basepath=ctx.scratch,
                           shared_basepath=ctx.shared)

        return {
            'repeat_id': self._inputs['repeat_id'],
            'generation': self._inputs['generation'],
            'simtype': 'solvent',
            **outputs
        }


class SepTopSolventRunUnit(BaseSepTopRunUnit):
    """
    Protocol Unit for the solvent phase of an relative SepTop free energy
    """
    def _get_components(self):
        """
        Get the relevant components for a solvent transformation.

        Note
        -----
        The solvent portion of the transformation is the transformation of one
        ligand into the other in the solvent. The only thing that
        should be present is the alchemical species in state A and state B
        and the SolventComponent.

        Returns
        -------
        alchem_comps : dict[str, Component]
          A list of alchemical components
        solv_comp : SolventComponent
          The SolventComponent of the system
        prot_comp : Optional[ProteinComponent]
          The protein component of the system, if it exists.
        small_mols : dict[SmallMoleculeComponent: OFFMolecule]
          SmallMoleculeComponents to add to the system.
        """
        stateA = self._inputs['stateA']
        alchem_comps = self._inputs['alchemical_components']

        small_mols_A = {m: m.to_openff()
                        for m in alchem_comps['stateA']}
        small_mols_B = {m: m.to_openff()
                        for m in alchem_comps['stateB']}
        small_mols = small_mols_A | small_mols_B

        solv_comp, _, _ = system_validation.get_components(stateA)

        # 1. We don't need to check that solv_comp is not None, otherwise
        # an error will have been raised when calling `validate_solvent`
        # in the Protocol's `_create`.
        # 2. ProteinComps can't be alchem_comps (for now), so will
        # be returned as None
        return alchem_comps, solv_comp, None, small_mols


    def _handle_settings(self) -> dict[str, SettingsBaseModel]:
        """
        Extract the relevant settings for a complex transformation.

        Returns
        -------
        settings : dict[str, SettingsBaseModel]
          A dictionary with the following entries:
            * forcefield_settings : OpenMMSystemGeneratorFFSettings
            * thermo_settings : ThermoSettings
            * charge_settings : OpenFFPartialChargeSettings
            * solvation_settings : OpenMMSolvationSettings
            * alchemical_settings : AlchemicalSettings
            * lambda_settings : LambdaSettings
            * engine_settings : OpenMMEngineSettings
            * integrator_settings : IntegratorSettings
            * equil_simulation_settings : MDSimulationSettings
            * equil_output_settings : MDOutputSettings
            * simulation_settings : MultiStateSimulationSettings
            * output_settings: MultiStateOutputSettings
            * restraint_settings: RestraintsSettings
        """
        prot_settings = self._inputs['protocol'].settings

        settings = {}
        settings['forcefield_settings'] = prot_settings.solvent_forcefield_settings
        settings['thermo_settings'] = prot_settings.thermo_settings
        settings['charge_settings'] = prot_settings.partial_charge_settings
        settings['solvation_settings'] = prot_settings.solvent_solvation_settings
        settings['alchemical_settings'] = prot_settings.alchemical_settings
        settings['lambda_settings'] = prot_settings.lambda_settings
        settings['engine_settings'] = prot_settings.solvent_engine_settings
        settings['integrator_settings'] = prot_settings.integrator_settings
        settings['equil_simulation_settings'] = prot_settings.solvent_equil_simulation_settings
        settings['equil_output_settings'] = prot_settings.solvent_equil_output_settings
        settings['simulation_settings'] = prot_settings.solvent_simulation_settings
        settings['output_settings'] = prot_settings.solvent_output_settings
        settings['restraint_settings'] = prot_settings.solvent_restraints_settings

        settings_validation.validate_timestep(
            settings['forcefield_settings'].hydrogen_mass,
            settings['integrator_settings'].timestep
        )

        return settings

    def _get_lambda_schedule(
            self, settings: dict[str, SettingsBaseModel]
    ) -> dict[str, npt.NDArray]:

        lambdas = dict()

        lambdas['lambda_electrostatics_ligandA'] = settings[
            'lambda_settings'].lambda_elec_ligandA
        lambdas['lambda_sterics_ligandA'] = settings[
            'lambda_settings'].lambda_vdw_ligandA
        lambdas['lambda_electrostatics_ligandB'] = settings[
            'lambda_settings'].lambda_elec_ligandB
        lambdas['lambda_sterics_ligandB'] = settings[
            'lambda_settings'].lambda_vdw_ligandB

        return lambdas

class SepTopComplexRunUnit(BaseSepTopRunUnit):
    """
    Protocol Unit for the solvent phase of an relative SepTop free energy
    """
    def _get_components(self):
        """
        Get the relevant components for a complex transformation.

        Returns
        -------
        alchem_comps : dict[str, Component]
          A list of alchemical components
        solv_comp : SolventComponent
          The SolventComponent of the system
        prot_comp : Optional[ProteinComponent]
          The protein component of the system, if it exists.
        small_mols : dict[SmallMoleculeComponent: OFFMolecule]
          SmallMoleculeComponents to add to the system.
        """
        stateA = self._inputs['stateA']
        alchem_comps = self._inputs['alchemical_components']

        solv_comp, prot_comp, small_mols = system_validation.get_components(stateA)
        small_mols = {m: m.to_openff() for m in small_mols}
        # Also get alchemical smc from state B
        small_mols_B = {m: m.to_openff()
                        for m in alchem_comps['stateB']}
        small_mols = small_mols | small_mols_B

        return alchem_comps, solv_comp, prot_comp, small_mols


    def _handle_settings(self) -> dict[str, SettingsBaseModel]:
        """
        Extract the relevant settings for a complex transformation.

        Returns
        -------
        settings : dict[str, SettingsBaseModel]
          A dictionary with the following entries:
            * forcefield_settings : OpenMMSystemGeneratorFFSettings
            * thermo_settings : ThermoSettings
            * charge_settings : OpenFFPartialChargeSettings
            * solvation_settings : OpenMMSolvationSettings
            * alchemical_settings : AlchemicalSettings
            * lambda_settings : LambdaSettings
            * engine_settings : OpenMMEngineSettings
            * integrator_settings : IntegratorSettings
            * equil_simulation_settings : MDSimulationSettings
            * equil_output_settings : MDOutputSettings
            * simulation_settings : SimulationSettings
            * output_settings: MultiStateOutputSettings
            * restraint_settings: RestraintsSettings
        """
        prot_settings = self._inputs['protocol'].settings

        settings = {
            'forcefield_settings': prot_settings.complex_forcefield_settings,
            'thermo_settings': prot_settings.thermo_settings,
            'charge_settings': prot_settings.partial_charge_settings,
            'solvation_settings': prot_settings.complex_solvation_settings,
            'alchemical_settings': prot_settings.alchemical_settings,
            'lambda_settings': prot_settings.lambda_settings,
            'engine_settings': prot_settings.complex_engine_settings,
            'integrator_settings': prot_settings.integrator_settings,
            'equil_simulation_settings':
                prot_settings.complex_equil_simulation_settings,
            'equil_output_settings':
                prot_settings.complex_equil_output_settings,
            'simulation_settings': prot_settings.complex_simulation_settings,
            'output_settings': prot_settings.complex_output_settings,
            'restraint_settings': prot_settings.complex_restraints_settings}

        settings_validation.validate_timestep(
            settings['forcefield_settings'].hydrogen_mass,
            settings['integrator_settings'].timestep
        )

        return settings

    def _get_lambda_schedule(
            self, settings: dict[str, SettingsBaseModel]
    ) -> dict[str, npt.NDArray]:
        lambdas = dict()

        lambdas['lambda_electrostatics_ligandA'] = settings[
            'lambda_settings'].lambda_elec_ligandA
        lambdas['lambda_sterics_ligandA'] = settings[
            'lambda_settings'].lambda_vdw_ligandA
        lambdas['lambda_restraints_ligandA'] = settings[
            'lambda_settings'].lambda_restraints_ligandA
        lambdas['lambda_electrostatics_ligandB'] = settings[
            'lambda_settings'].lambda_elec_ligandB
        lambdas['lambda_sterics_ligandB'] = settings[
            'lambda_settings'].lambda_vdw_ligandB
        lambdas['lambda_restraints_ligandB'] = settings[
            'lambda_settings'].lambda_restraints_ligandB

        return lambdas