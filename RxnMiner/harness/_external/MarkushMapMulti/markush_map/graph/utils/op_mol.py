from collections import deque
from typing import Tuple, Dict, List, Set, Optional

from rdkit import Chem

from .parse_regex import index_map_num

SEARCH_TYPE = Tuple[str, Dict[int, str], Dict[int, int]]
RESULT_TYPE = List[SEARCH_TYPE]


def add_hydrogens_with_mapping(mol: Chem.Mol) -> Tuple[Chem.Mol, Dict[int, int]]:
    mol_copy = Chem.Mol(mol)
    num_original_atoms = mol_copy.GetNumAtoms()
    mol_with_h = Chem.AddHs(mol_copy)
    atom_mapping = {
        new_idx: new_idx if new_idx < num_original_atoms else -1
        for new_idx in range(mol_with_h.GetNumAtoms())
    }
    return mol_with_h, atom_mapping


def get_mol_matches_with_query_hs(
        complete_mol: Chem.Mol,
        query_mol: Chem.Mol,
) -> List[List[int]]:
    complete_mol_with_hs, dict_hs_map = add_hydrogens_with_mapping(complete_mol)
    matches: List[List[int]] = complete_mol_with_hs.GetSubstructMatches(
        query_mol,
        uniquify=0,
        maxMatches=24,
        useChirality=False,
        useQueryQueryMatches=True,
    )
    matches = [[dict_hs_map[idx] for idx in match] for match in matches]
    return matches


def get_mol_matches_with_query(
        complete_mol: Chem.Mol,
        query_mol: Chem.Mol,
) -> List[List[int]]:
    matches: List[List[int]] = complete_mol.GetSubstructMatches(
        query_mol,
        uniquify=0,
        maxMatches=24,
        useChirality=False,
        useQueryQueryMatches=True,
    )
    return matches


def extract_group(complete_mol: Chem.Mol, attachment_point_idx: int, skeleton_atoms: Set[int]) -> Set[int]:
    if attachment_point_idx == -1:
        return set()
    visited = set()
    queue = deque([attachment_point_idx])
    while queue:
        current_idx = queue.popleft()
        if current_idx in visited:
            continue
        visited.add(current_idx)
        current_atom = complete_mol.GetAtomWithIdx(current_idx)

        for neighbor in current_atom.GetNeighbors():
            neighbor_idx = neighbor.GetIdx()
            if neighbor_idx not in skeleton_atoms:
                queue.append(neighbor_idx)
    return visited


def extract_mol_subset_smiles(
        mol: Chem.Mol, indices: Set[int], dict_idx_label: Dict[int, str],
) -> SEARCH_TYPE:
    if len(indices) == 0:
        raise ValueError(f"data.utils.op_mol.extract_mol_subset_smiles: empty indices {indices}")
    subset_mol = Chem.RWMol()
    atom_map: Dict[int, int] = {}
    r_map: Dict[int, str] = {}

    def _pre_add(_idx: int) -> Optional[int]:
        if _idx in dict_idx_label:
            _star_atom = Chem.Atom('*')
            _star_idx = subset_mol.AddAtom(_star_atom)
            r_map[_star_idx] = dict_idx_label[_idx]
            return _star_idx
        else:
            return None

    def _post_add(_idx: int, _star_idx: Optional[int]):
        if _idx in dict_idx_label:
            subset_mol.AddBond(_star_idx, new_idx, Chem.BondType.SINGLE)

    star_flag = False
    for orig_idx in indices:
        atom = mol.GetAtomWithIdx(orig_idx)
        if not star_flag:
            star_idx = _pre_add(orig_idx)
        else:
            star_idx = None
        new_atom = Chem.Atom(atom.GetAtomicNum())
        new_atom.SetFormalCharge(atom.GetFormalCharge())
        new_atom.SetIsAromatic(atom.GetIsAromatic())
        new_idx = subset_mol.AddAtom(new_atom)
        atom_map[orig_idx] = new_idx
        if star_idx is not None:
            star_flag = True
        else:
            star_idx = _pre_add(orig_idx)
        _post_add(orig_idx, star_idx)
    for bond in mol.GetBonds():
        begin_idx = bond.GetBeginAtomIdx()
        end_idx = bond.GetEndAtomIdx()
        if begin_idx in indices and end_idx in indices:
            subset_mol.AddBond(
                atom_map[begin_idx],
                atom_map[end_idx],
                bond.GetBondType()
            )
    subset_mol = subset_mol.GetMol()
    base_smiles, reversed_indices = get_smiles_with_atom_map(subset_mol)
    return base_smiles, {reversed_indices[idx]: label for idx, label in r_map.items()}, atom_map


def get_groups_info_with_match(
        complete_mol: Chem.Mol,
        match: List[int],
        dict_group: Dict[int, str],
) -> Optional[RESULT_TYPE]:
    skeleton_atoms = set(match) - set([match[idx] for idx in dict_group.keys()])
    tuples_idx_label_indices = []
    for idx, label in dict_group.items():
        if idx < len(match):
            attachment_point_idx = match[idx]
            group_indices = extract_group(complete_mol, attachment_point_idx, skeleton_atoms)
            tuples_idx_label_indices.append((attachment_point_idx, label, group_indices))

    # 同构性判断
    dict_label_groups_indices: Dict[str, List[Set[int]]] = {}
    for _, label, group_indices in tuples_idx_label_indices:
        dict_label_groups_indices.setdefault(label, []).append(group_indices)
    for label, groups_indices in dict_label_groups_indices.items():
        if len(groups_indices) == 1:
            continue
        atom_texts: List[str] = []
        for indices in groups_indices:
            atom_texts.append("".join(sorted([complete_mol.GetAtomWithIdx(idx).GetSymbol() for idx in indices])))
        if len(set(atom_texts)) != 1:
            return None

    # 合并相连基团，但是不合并空基团
    dict_indices_dict_idx_label: Dict[Tuple, Dict[int, str]] = {}
    empty_labels: List[str] = []
    for idx, r_label, r_group_indices in tuples_idx_label_indices:
        sorted_indices = tuple(sorted(r_group_indices))
        if len(sorted_indices):
            dict_indices_dict_idx_label.setdefault(sorted_indices, {})[idx] = r_label
        else:
            empty_labels.append(r_label)
    # print(dict_indices_dict_idx_label)
    # print(empty_labels)

    # 基团去重
    exist_labels: Set[str] = set()
    new_dict: Dict[Tuple, Dict[int, str]] = {}
    for sorted_indices, dict_idx_label in dict_indices_dict_idx_label.items():
        if len(exist_labels & set(dict_idx_label.values())):
            continue
        for label in dict_idx_label.values():
            exist_labels.add(label)
        new_dict[sorted_indices] = dict_idx_label

    return [
        extract_mol_subset_smiles(complete_mol, set(sorted_indices), dict_idx_label)
        for sorted_indices, dict_idx_label in new_dict.items()
    ] + [(f"*", {0: label}, {}) for label in empty_labels]


def connect_mols(mol1, mol2, atom_pairs, bond_type=Chem.BondType.SINGLE) -> Tuple[Chem.Mol, List[Tuple[int, int]]]:
    # 确保输入是分子对象
    if not isinstance(mol1, Chem.Mol) or not isinstance(mol2, Chem.Mol):
        raise TypeError("data.utils.op_mol.connect_mols: input mol1 and mol2 should be Chem.Mol")

    # 确保原子索引有效
    for atom_idx1, atom_idx2 in atom_pairs:
        if atom_idx1 >= mol1.GetNumAtoms() or atom_idx1 < 0:
            raise ValueError(f"原子索引 {atom_idx1} 超出mol1的范围")
        if atom_idx2 >= mol2.GetNumAtoms() or atom_idx2 < 0:
            raise ValueError(f"原子索引 {atom_idx2} 超出mol2的范围")

    # 首先合并两个分子（不添加键）
    combo = Chem.CombineMols(mol1, mol2)

    # 创建可编辑的分子
    rwmol = Chem.RWMol(combo)
    conn_pairs = []

    # 添加键连接两个分子
    # 注意：第二个分子的原子索引需要偏移第一个分子的原子数量
    for atom_idx1, atom_idx2 in atom_pairs:
        conn_pair = atom_idx1, atom_idx2 + mol1.GetNumAtoms()
        rwmol.AddBond(*conn_pair, bond_type)
        conn_pairs.append(conn_pair)

    # 获取连接的分子
    connected_mol = rwmol.GetMol()

    # # 尝试清理结构（检查价态等）
    # try:
    #     Chem.SanitizeMol(connected_mol)
    # except:
    #     print("警告：分子消毒失败，返回的分子可能不是化学合理的")

    return connected_mol, conn_pairs


def squeeze_conn_pairs(mol: Chem.Mol, conn_pairs: List[Tuple[int, int]]) -> Tuple[Chem.Mol, Dict[int, int]]:
    # 创建可编辑的分子副本
    rwmol = Chem.RWMol(mol)
    for atom in rwmol.GetAtoms():
        atom.SetAtomMapNum(atom.GetIdx() + 1)

    # 记录需要删除的原子和新建的连接
    atoms_to_delete = set()
    new_bonds = []

    # 按照降序排列连接对，以便从后往前删除（避免索引变化问题）
    sorted_conn_pairs = sorted(conn_pairs, key=lambda x: max(x[0], x[1]), reverse=True)
    # print(sorted_conn_pairs)

    # 处理每个连接对
    for atom_idx1, atom_idx2 in sorted_conn_pairs:
        # 获取原子对象
        atom1 = rwmol.GetAtomWithIdx(atom_idx1)
        atom2 = rwmol.GetAtomWithIdx(atom_idx2)

        # 找到atom1和atom2的其他邻居（除了彼此）
        neighbors1 = [n.GetIdx() for n in atom1.GetNeighbors() if n.GetIdx() != atom_idx2]
        neighbors2 = [n.GetIdx() for n in atom2.GetNeighbors() if n.GetIdx() != atom_idx1]
        # print(neighbors1, neighbors2)

        # 如果邻居尚未被标记为删除，则添加新连接
        for neighbor_idx1 in neighbors1:
            for neighbor_idx2 in neighbors2:
                if neighbor_idx1 not in atoms_to_delete and neighbor_idx2 not in atoms_to_delete:
                    new_bonds.append((neighbor_idx1, neighbor_idx2))

        # 标记原子对为待删除
        atoms_to_delete.add(atom_idx1)
        atoms_to_delete.add(atom_idx2)

    # print(new_bonds)
    # 添加新的连接
    for atom_idx1, atom_idx2 in new_bonds:
        bond = rwmol.GetBondBetweenAtoms(atom_idx1, atom_idx2)
        if bond is None:
            rwmol.AddBond(atom_idx1, atom_idx2, Chem.BondType.SINGLE)

    # 删除原子（必须从高索引到低索引删除，以避免索引变化）
    for idx in sorted(atoms_to_delete, reverse=True):
        rwmol.RemoveAtom(idx)

    # 返回处理后的分子
    atom_map = {}
    for atom in rwmol.GetAtoms():
        atom_map[atom.GetAtomMapNum() - 1] = atom.GetIdx()
        atom.SetAtomMapNum(0)
    return rwmol.GetMol(), atom_map


def get_mol_atom_hs(mol: Chem.Mol) -> Dict[int, int]:
    return {atom.GetIdx(): atom.GetTotalNumHs() for atom in mol.GetAtoms()}


def get_smiles_with_atom_map(mol: Chem.Mol) -> Tuple[str, Dict[int, int]]:
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(atom.GetIdx() + 1)
    smiles = Chem.MolToSmiles(mol, rootedAtAtom=0, canonical=False, isomericSmiles=True)
    atom_map = {k - 1: v for k, v in index_map_num(smiles).items()}
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)
    smiles = Chem.MolToSmiles(mol, rootedAtAtom=0, canonical=False, isomericSmiles=True)
    return smiles, atom_map
