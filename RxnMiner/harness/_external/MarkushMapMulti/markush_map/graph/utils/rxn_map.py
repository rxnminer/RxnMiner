from typing import List, Tuple, Dict

from rdkit import Chem
from rxnmapper import RXNMapper

if __name__ == '__main__':
    from atom_map import get_atom_mapping_mcs
    from common import get_nums_indices
else:
    from .atom_map import get_atom_mapping_mcs
    from .common import get_nums_indices


def get_smiles_unique_map(
        reactants_smile: List[str], products_smiles: List[str],
) -> Tuple[List[Dict[int, int]], List[Dict[int, int]]]:
    # print(reactants_smile)
    # print(products_smiles)
    tuples_map = get_atom_map_rxn(reactants_smile, products_smiles)
    # print(tuples_map)
    reactants_dict_idx_to_unique: List[Dict[int, int]] = [{} for _ in range(len(reactants_smile))]
    products_dict_idx_to_unique: List[Dict[int, int]] = [{} for _ in range(len(products_smiles))]
    for unique_id, ((ri, rj), (pi, pj)) in enumerate(tuples_map):
        reactants_dict_idx_to_unique[ri][rj] = unique_id
        products_dict_idx_to_unique[pi][pj] = unique_id
    return reactants_dict_idx_to_unique, products_dict_idx_to_unique


def get_atom_map_rxn(
        reactants_smiles: List[str],
        products_smiles: List[str],
) -> List[Tuple[Tuple[int, int], Tuple[int, int]]]:
    in_r_smiles = ".".join([s for s in reactants_smiles if s])
    in_p_smiles = ".".join([s for s in products_smiles if s])
    intput_expression = f"{in_r_smiles}>>{in_p_smiles}"
    rxn_mapper = RXNMapper()
    output = rxn_mapper.get_attention_guided_atom_maps([intput_expression])[0]
    output_expression = output["mapped_rxn"]
    out_r_smiles, out_p_smiles = output_expression.split(">>")
    dict_in_r_to_out_r = get_atom_mapping_mcs(in_r_smiles, out_r_smiles)
    dict_out_p_to_in_p = get_atom_mapping_mcs(out_p_smiles, in_p_smiles)
    out_r_mol, out_p_mol = Chem.MolFromSmiles(out_r_smiles), Chem.MolFromSmiles(out_p_smiles)
    dict_out_r_to_unique = {
        atom.GetIdx(): atom.GetAtomMapNum() for atom in out_r_mol.GetAtoms() if atom.GetAtomMapNum() != 0}
    dict_unique_to_out_p = {
        atom.GetAtomMapNum(): atom.GetIdx() for atom in out_p_mol.GetAtoms() if atom.GetAtomMapNum() != 0}
    dict_in_r_to_in_p: Dict[int, int] = {}
    # print(in_r_smiles)
    # print(dict_in_r_to_out_r)
    # print(out_r_smiles)
    # print(dict_out_r_to_unique)
    # print(dict_unique_to_out_p)
    # print(out_p_smiles)
    # print(dict_out_p_to_in_p)
    # print(in_p_smiles)
    for in_r_id in dict_in_r_to_out_r.keys():
        out_r_id = dict_in_r_to_out_r[in_r_id]
        unique_id = dict_out_r_to_unique.get(out_r_id, 0)
        if unique_id == 0:
            continue
        out_p_id = dict_unique_to_out_p[unique_id]
        in_p_id = dict_out_p_to_in_p[out_p_id]
        dict_in_r_to_in_p[in_r_id] = in_p_id
    # for k, v in dict_in_r_to_in_p.items():
    #     print(k, v)
    nums_r_atom = [Chem.MolFromSmiles(smiles).GetNumAtoms() if len(smiles) else 0 for smiles in reactants_smiles]
    nums_p_atom = [Chem.MolFromSmiles(smiles).GetNumAtoms() if len(smiles) else 0 for smiles in products_smiles]
    r_tuple = get_nums_indices(nums_r_atom)
    p_tuple = get_nums_indices(nums_p_atom)
    return [(r_tuple[r], p_tuple[p]) for r, p in dict_in_r_to_in_p.items()]


if __name__ == '__main__':
    result = get_atom_map_rxn([
        "C(/C=C/c1ccccc1)(=O)O",
        "*/C(=C(/Br)\*)/*",
    ], [
        "C1C=CC=C(/C=C/C(=C(/*)\*)/*)C=1",
    ])
    print(result)
