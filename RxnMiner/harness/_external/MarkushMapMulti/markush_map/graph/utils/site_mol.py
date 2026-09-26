from typing import Set, Dict, Tuple

from rdkit import Chem


def site_mol(mol: Chem.Mol, sited_indices: Set[int]) -> Tuple[Chem.Mol, Dict[int, int], Dict[int, int]]:
    rwmol = Chem.RWMol(mol)
    atom_map, site_map = {i: i for i in range(rwmol.GetNumAtoms())}, {}
    for idx in sited_indices:
        new_idx = rwmol.AddAtom(Chem.Atom("*"))
        rwmol.AddBond(idx, new_idx, Chem.BondType.SINGLE)
        site_map[idx] = new_idx
    return rwmol.GetMol(), atom_map, site_map


# Example usage:
if __name__ == "__main__":
    # Create a sample molecule
    mol = Chem.MolFromSmiles("CCO")
    print("Original SMILES:", Chem.MolToSmiles(mol))
    print("Original atom count:", mol.GetNumAtoms())

    # Specify atoms to replace H with *
    sited_indices = {0, 2}  # Replace H atoms on the first C and the O

    # Apply the function
    new_mol, atom_map, site_map = site_mol(mol, sited_indices)

    print("Modified SMILES:", Chem.MolToSmiles(new_mol))
    print("Modified atom count:", new_mol.GetNumAtoms())
    print("Atom mapping:", atom_map)
    print("Site mapping:", site_map)

    # Print the atoms in the new molecule
    print("\nAtoms in the new molecule:")
    for atom in new_mol.GetAtoms():
        print(f"Atom {atom.GetIdx()}: {atom.GetSymbol()}")

    # Print the bonds in the new molecule
    print("\nBonds in the new molecule:")
    for bond in new_mol.GetBonds():
        begin_atom = bond.GetBeginAtomIdx()
        end_atom = bond.GetEndAtomIdx()
        begin_symbol = new_mol.GetAtomWithIdx(begin_atom).GetSymbol()
        end_symbol = new_mol.GetAtomWithIdx(end_atom).GetSymbol()
        print(f"Bond: {begin_atom}({begin_symbol}) - {end_atom}({end_symbol})")

    # Verify the site map is correct
    print("\nVerifying wildcard atoms:")
    for orig_idx, wildcard_idx in site_map.items():
        atom = new_mol.GetAtomWithIdx(wildcard_idx)
        parent_idx = None
        for bond in new_mol.GetBonds():
            if bond.GetBeginAtomIdx() == wildcard_idx or bond.GetEndAtomIdx() == wildcard_idx:
                other_idx = bond.GetBeginAtomIdx() if bond.GetEndAtomIdx() == wildcard_idx else bond.GetEndAtomIdx()
                if other_idx in atom_map.values():
                    for k, v in atom_map.items():
                        if v == other_idx:
                            parent_idx = k
                            break
        print(f"Wildcard at {wildcard_idx} is connected to original atom {orig_idx}, verified parent: {parent_idx}")
