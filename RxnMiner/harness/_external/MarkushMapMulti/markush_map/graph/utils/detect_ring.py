from collections import defaultdict
from typing import Dict, Set, Union

from rdkit import Chem


def find_ring_atoms(smiles_or_mol: Union[str, Chem.Mol], ring_number: int = None) -> Dict[int, Set[int]]:
    """
    Identify atoms belonging to each ring in a molecule from SMILES.

    Parameters:
    -----------
    smiles_or_mol : str | Mol
        The SMILES string of the molecule
    ring_number : int, optional
        If specified, returns only the atoms for the Nth ring (1-indexed)

    Returns:
    --------
    Dict[int, Set[int]]
        A dictionary mapping ring indices (0-indexed) to sets of atom indices belonging to each ring
        If ring_number is specified, returns only that ring's atoms
    """
    # Create mol object from SMILES
    if isinstance(smiles_or_mol, str):
        mol = Chem.MolFromSmiles(smiles_or_mol)
        if mol is None:
            raise ValueError(f"Invalid SMILES string: {smiles_or_mol}")
    else:
         mol = smiles_or_mol

    # Get the ring information
    rings = Chem.GetSSSR(mol)

    # Create dictionary of ring atoms
    ring_atoms = {}
    for i, ring in enumerate(rings):
        # Convert ring to a set of atom indices
        atom_indices = set()
        for atom_idx in ring:
            atom_indices.add(atom_idx)
        ring_atoms[i] = atom_indices

    if ring_number is not None:
        if ring_number <= 0 or ring_number > len(ring_atoms):
            raise ValueError(f"Ring number {ring_number} is out of range. Molecule has {len(ring_atoms)} rings.")
        return {ring_number: ring_atoms[ring_number]}

    return ring_atoms


def visualize_rings(smiles: str) -> None:
    """
    Visualize the rings in a molecule with their indices.

    Parameters:
    -----------
    smiles : str
        The SMILES string of the molecule
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        print(f"Invalid SMILES string: {smiles}")
        return

    # Get the ring atoms
    ring_atoms = find_ring_atoms(smiles)

    # Create a dictionary to track which atoms belong to which rings
    atom_to_rings = defaultdict(list)
    for ring_idx, atoms in ring_atoms.items():
        for atom_idx in atoms:
            atom_to_rings[atom_idx].append(ring_idx + 1)  # Convert to 1-indexed for display

    # Print information
    print(f"SMILES: {smiles}")
    print(f"Number of rings: {len(ring_atoms)}")

    for ring_idx, atoms in ring_atoms.items():
        print(f"Ring {ring_idx + 1} contains atoms: {sorted(atoms)}")

    print("\nSummary of atoms and their rings:")
    for atom_idx in sorted(atom_to_rings.keys()):
        atom = mol.GetAtomWithIdx(atom_idx)
        symbol = atom.GetSymbol()
        print(f"Atom {atom_idx} ({symbol}) belongs to rings: {atom_to_rings[atom_idx]}")


# Example usage
if __name__ == "__main__":
    test_smiles = "C(#CC1CCCCC1)c1ccc2[nH]ccc2c1"
    visualize_rings(test_smiles)

    # Get atoms for a specific ring
    ring_number = 2  # Get the second ring
    specific_ring = find_ring_atoms(test_smiles, ring_number)
    print(f"\nAtoms in ring {ring_number}: {sorted(list(specific_ring[ring_number - 1]))}")
