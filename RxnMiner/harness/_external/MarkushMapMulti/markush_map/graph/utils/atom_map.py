from typing import Dict

from rdkit import Chem


def get_atom_mapping_mcs(smiles1: str, smiles2: str) -> Dict[int, int]:
    # Parse SMILES into molecules
    # smiles1 = smiles1.replace("*", "C")
    # smiles2 = smiles2.replace("*", "C")
    # print(smiles1)
    # print(smiles2)
    mol1 = Chem.MolFromSmiles(smiles1)
    mol2 = Chem.MolFromSmiles(smiles2)
    # print(mol1)
    # print(mol2)

    # Get atom mappings from MCS
    match = mol2.GetSubstructMatch(mol1)

    # Create mapping dictionary
    mapping = {}
    for i, j in enumerate(match):
        mapping[i] = j

    return mapping


if __name__ == '__main__':
    _mapping = get_atom_mapping_mcs(
        "C(C=Cc1ccccc1)(=O)O.C(=C(Br)*)(*)*",
        "BrC(=C(*)*)*.O=C(O)C=Cc1ccccc1",
    )
    print(_mapping)
