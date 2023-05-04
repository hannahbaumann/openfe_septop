# This code is part of OpenFE and is licensed under the MIT license.
# For details, see https://github.com/OpenFreeEnergy/openfe
"""
Reusable utility methods to create Systems for OpenMM-based alchemical
Protocols.
"""
import numpy as np
import numpy.typing as npt
from openmm import app, MonteCarloBarostat
from openmm import unit as omm_unit
from openff.toolkit import Molecule as OFFMol
from openff.units.openmm import to_openmm, ensure_quantity
from openmmforcefields.generators import SystemGenerator
from typing import Dict, List, Tuple, Optional
from pathlib import Path
from gufe.settings import OpenMMSystemGeneratorFFSettings, ThermoSettings
from gufe import (
    Component, ProteinComponent, SolventComponent, SmallMoleculeComponent
)
from ..openmm_rfe.equil_rfe_settings import (
    SystemSettings, SimulationSettings, SolvationSettings
)


def get_system_generator(
    forcefield_settings: OpenMMSystemGeneratorFFSettings,
    thermo_settings: ThermoSettings,
    system_settings: SystemSettings,
    cache: Path,
    has_solvent: bool,
) -> SystemGenerator:
    """
    Create a SystemGenerator based on Protocol settings.

    Paramters
    ---------
    forcefield_settings : OpenMMSystemGeneratorFFSettings
      Force field settings, including necessary information
      for constraints, hydrogen mass, rigid waters, COM removal,
      non-ligand FF xmls, and the ligand FF name.
    thermo_settings : ThermoSettings
      Thermodynamic settings, including everything necessary to
      create a barostat.
    system_settings : SystemSettings
      System settings including all necessary information for
      the nonbonded methods.
    cache : pathlib.Path
      Path to openff force field cache.
    has_solvent : bool
      Wether or not the target system has solvent (and by extension
      might require a barostat).

    Returns
    -------
    system_generator : openmmforcefields.generator.SystemGenerator
      System Generator to use for this Protocol.

    TODO
    ----
    * Investigate how RF can be passed to non-periodic kwargs.
    """
    # get the right constraint
    constraints = {
        'hbonds': app.HBonds,
        'none': None,
        'allbonds': app.AllBonds,
        'hangles': app.HAngles
        # vvv can be None so string it
    }[str(forcefield_settings.constraints).lower()]

    # create forcefield_kwargs entry
    forcefield_kwargs = {
        'constraints': constraints,
        'rigidWater': forcefield_settings.rigid_water,
        'removeCMMotion': forcefield_settings.remove_com,
        'hydrogenMass': forcefield_settings.hydrogen_mass * omm_unit.amu,
    }

    # get the right nonbonded method
    nonbonded_method = {
        'pme': app.PME,
        'nocutoff': app.NoCutoff,
        'cutoffnonperiodic': app.CutoffNonPeriodic,
        'cutoffperiodic': app.CutoffPeriodic,
        'ewald': app.Ewald
    }[system_settings.nonbonded_method.lower()]

    nonbonded_cutoff = to_openmm(
        system_settings.nonbonded_cutoff
    )

    # create the periodic_kwarg entry
    periodic_kwargs = {
        'nonbondedMethod': nonbonded_method,
        'nonbondedCutoff': nonbonded_cutoff,
    }

    # Currently the else is a dead branch, we will want to investigate the
    # possibility of using CutoffNonPeriodic at some point though (for RF)
    if nonbonded_method is not app.CutoffNonPeriodic:
        nonperiodic_kwargs = {
                'nonbondedMethod': app.NoCutoff,
        }
    else:  # pragma: no-cover
        nonperiodic_kwargs = periodic_kwargs

    system_generator = SystemGenerator(
        forcefields=forcefield_settings.forcefields,
        small_molecule_forcefield=forcefield_settings.small_molecule_forcefield,
        forcefield_kwargs=forcefield_kwargs,
        nonperiodic_forcefield_kwargs=nonperiodic_kwargs,
        periodic_forcefield_kwargs=periodic_kwargs,
        cache=str(cache),
    )

    # Add a barostat if necessary
    # TODO: move this to its own place where we can then handle membranes
    # Note: this behaviour was broken pre 0.11.2 of openmmff
    if has_solvent:
        barostat = MonteCarloBarostat(
            ensure_quantity(thermo_settings.pressure, 'openmm'),
            ensure_quantity(thermo_settings.temperature, 'openmm'),
        )
        system_generator.barostat = barostat

    return system_generator


ModellerReturn = Tuple[app.Modeller, Dict[Component, npt.NDArray]]


def get_omm_modeller(protein_comp: Optional[ProteinComponent],
                     solvent_comp: Optional[SolventComponent],
                     small_mols: Dict[Component, OFFMol],
                     omm_forcefield : app.ForceField,
                     solvent_settings : SolvationSettings) -> ModellerReturn:
    """
    Generate an OpenMM Modeller class based on a potential input ProteinComponent,
    SolventComponent, and a set of small molecules.

    Parameters
    ----------
    protein_comp : Optional[ProteinComponent]
      Protein Component, if it exists.
    solvent_comp : Optional[ProteinCompoinent]
      Solvent Component, if it exists.
    small_mols : Dict[Component, openff.toolkit.Molecule]
      Dictionary of SmallMoleculeComponents and their associated
      OpenFF Molecule.
    omm_forcefield : app.ForceField
      ForceField object for system.
    solvent_settings : SolvationSettings
      Solvation settings.

    Returns
    -------
    system_modeller : app.Modeller
      OpenMM Modeller object generated from ProteinComponent and
      OpenFF Molecules.
    component_resids : Dict[Component, npt.NDArray]
      Dictionary of residue indices for each component in system.
    """
    component_resids = {}

    def _add_small_mol(comp: Component, mol: OFFMol,
                       system_modeller: app.Modeller,
                       comp_resids: Dict[Component, npt.NDArray]):
        """
        Helper method to add OFFMol to an existing Modeller object and
        update a dictionary tracking residue indices for each component.
        """
        omm_top = mol.to_topology().to_openmm()
        system_modeller.add(
            omm_top,
            ensure_quantity(mol.conformers[0], 'openmm')
        )

        nres = omm_top.getNumResidues()
        resids = [res.index for res in system_modeller.topology.residues()]
        comp_resids[comp] = np.array(resids[-nres:])

    # If there's a protein in the system, we add it first to the Modeller
    if protein_comp is not None:
        system_modeller = app.Modeller(protein_comp.to_openmm_topology(),
                                       protein_comp.to_openmm_positions())
        component_resids[protein_comp] = np.array(
          [r.index for r in system_modeller.topology.residues()]
        )

        for comp, mol in small_mols.items():
            _add_small_mol(comp, mol, system_modeller, component_resids)

    # Otherwise we add the first molecule and then the rest
    else:
        mol_items = list(small_mols.items())

        system_modeller = app.Modeller(
            mol_items[0][1].to_topology().to_openmm(),
            ensure_quantity(mol_items[0][1].conformers[0], 'openmm')
        )

        component_resids[mol_items[0][0]] = np.array(
            [r.index for r in system_modeller.topology.residues()]
        )

        for comp, mol in mol_items[1:]:
            _add_small_mol(comp, mol, system_modeller, component_resids)

    # Add solvent if neeeded
    if solvent_comp is not None:
        conc = solvent_comp.ion_concentration
        pos = solvent_comp.positive_ion
        neg = solvent_comp.negative_ion

        system_modeller.addSolvent(
            omm_forcefield,
            model=solvent_settings.solvent_model,
            padding=to_openmm(solvent_settings.solvent_padding),
            positiveIon=pos, negativeIon=neg,
            ionicStrength=to_openmm(conc)
        )

        all_resids = np.array(
            [r.index for r in system_modeller.topology.residues()]
        )

        existing_resids = np.concatenate(
            [resids for resids in component_resids.values()]
        )

        component_resids[solvent_comp] = np.setdiff1d(
            all_resids, existing_resids
        )

    return system_modeller, component_resids
