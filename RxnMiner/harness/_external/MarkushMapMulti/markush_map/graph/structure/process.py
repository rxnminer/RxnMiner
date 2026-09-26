from typing import Dict, Optional, Tuple

from rdkit import Chem
from rdkit.Chem import rdchem

from .atom import Atom
from .bond import Bond, BondType
from .mol_graph import MolGraph
from ..utils.op_mol import get_smiles_with_atom_map

DICT_BOND_TYPE: Dict[BondType, int] = {
    BondType.SINGLE: Chem.BondType.SINGLE,
    BondType.DOUBLE: Chem.BondType.DOUBLE,
    BondType.TRIPLE: Chem.BondType.TRIPLE,
    BondType.AROMATIC: Chem.BondType.AROMATIC,
}
DICT_BOND_TYPE_REVERSE: Dict[int, BondType] = {
    Chem.BondType.SINGLE: BondType.SINGLE,
    Chem.BondType.DOUBLE: BondType.DOUBLE,
    Chem.BondType.TRIPLE: BondType.TRIPLE,
    Chem.BondType.AROMATIC: BondType.AROMATIC,
}


def graph_to_query(mol_graph: MolGraph) -> Tuple[Chem.Mol, Dict[str, int]]:
    mol, atom_id_to_idx = graph_to_mol(mol_graph)
    smiles, atom_map = get_smiles_with_atom_map(mol)
    query = Chem.MolFromSmarts(smiles)
    return query, {k: atom_map[v] for k, v in atom_id_to_idx.items()}


def graph_to_mol(mol_graph: MolGraph) -> Tuple[Chem.Mol, Dict[str, int]]:
    # 创建一个可编辑的分子对象
    rw_mol = Chem.RWMol()

    # 跟踪MolGraph中的原子ID与RWMol中的原子索引的映射
    atom_id_to_idx: Dict[str, int] = {}

    # 添加原子
    for atom_id, atom in mol_graph.atoms.items():
        # 创建RDKit原子对象
        rd_atom = Chem.Atom(atom.symbol)

        # 设置其他属性
        if atom.has_property("charge"):
            rd_atom.SetFormalCharge(int(atom.get_property("charge")))
        if atom.has_property("chirality"):
            # 这里可以根据需要处理手性
            chirality = atom.get_property("chirality")
            # 实现手性设置...
        if atom.has_property("isotope"):
            rd_atom.SetIsotope(int(atom.get_property("isotope")))
        if atom.has_property("radical_electrons"):
            rd_atom.SetNumRadicalElectrons(int(atom.get_property("radical_electrons")))
        if atom.has_property("aromatic"):
            rd_atom.SetIsAromatic(bool(atom.get_property("aromatic")))
        if atom.has_property("num_explicit_hs"):
            rd_atom.SetNumExplicitHs(bool(atom.get_property("num_explicit_hs")))

        # 将原子添加到分子中
        atom_idx = rw_mol.AddAtom(rd_atom)
        atom_id_to_idx[atom_id] = atom_idx

    # 添加键
    for bond_id, bond in mol_graph.bonds.items():
        # 获取键的起点和终点原子索引
        begin_idx = atom_id_to_idx.get(bond.source)
        end_idx = atom_id_to_idx.get(bond.target)

        if begin_idx is None or end_idx is None:
            continue  # 忽略连接到不存在原子的键

        # 确定键类型
        bond_type = DICT_BOND_TYPE.get(bond.type, Chem.BondType.SINGLE)

        # 添加键到分子中
        rw_mol.AddBond(begin_idx, end_idx, bond_type)

        # 设置键属性
        if bond.has_property("stereo"):
            stereo = bond.get_property("stereo")
            # 实现立体化学设置...
            # 例如: rw_mol.GetBondBetweenAtoms(begin_idx, end_idx).SetStereo(...)
        if bond.has_property("aromatic"):
            rw_mol.GetBondBetweenAtoms(begin_idx, end_idx).SetIsAromatic(bool(bond.get_property("aromatic")))

    # 转换为不可变的Mol对象
    mol = rw_mol.GetMol()

    # 清理分子（添加氢原子、设定芳香性等）
    # try:
    #     mol = Chem.AddHs(mol)
    #     Chem.SanitizeMol(mol)
    #     mol = Chem.RemoveHs(mol)  # 通常在转换为SMILES之前移除显式氢原子
    # except Exception as e:
    #     print(f"警告: 分子清理失败: {e}")
    #     # 继续使用未清理的分子

    return mol, atom_id_to_idx


def graph_to_smiles(mol_graph: MolGraph) -> Tuple[str, Dict[str, int]]:
    mol, atom_id_to_idx = graph_to_mol(mol_graph)
    smiles, atom_map = get_smiles_with_atom_map(mol)
    return smiles, {k: atom_map[v] for k, v in atom_id_to_idx.items()}


def mol_to_graph(
        mol: Chem.Mol,
        name: Optional[str] = None,
        include_hs: bool = False
) -> Tuple[MolGraph, Dict[int, str]]:
    if name is None:
        name = Chem.MolToSmiles(mol)
    # 如果需要处理氢原子，先确保所有氢原子都是显式的
    if include_hs:
        mol = Chem.AddHs(mol)
    elif not include_hs:
        mol = Chem.RemoveHs(mol)

    # 创建一个新的分子图
    mol_graph = MolGraph()
    atom_idx_to_id: Dict[int, str] = {}

    # 处理原子
    for atom_idx in range(mol.GetNumAtoms()):
        rd_atom = mol.GetAtomWithIdx(atom_idx)

        # 创建原子ID
        atom_id = f"{name}_{atom_idx}"

        # 收集原子属性
        properties = {
            "charge": rd_atom.GetFormalCharge(),
            "aromatic": rd_atom.GetIsAromatic(),
            "hybrid": str(rd_atom.GetHybridization()),
            "num_explicit_hs": rd_atom.GetNumExplicitHs(),
            "num_implicit_hs": rd_atom.GetNumImplicitHs(),
            "total_valence": rd_atom.GetTotalValence(),
            "atomic_num": rd_atom.GetAtomicNum(),
        }

        # 处理可选属性
        if rd_atom.GetIsotope() > 0:
            properties["isotope"] = rd_atom.GetIsotope()

        if rd_atom.GetNumRadicalElectrons() > 0:
            properties["radical_electrons"] = rd_atom.GetNumRadicalElectrons()

        # 处理手性
        chiral_tag = rd_atom.GetChiralTag()
        if chiral_tag != rdchem.ChiralType.CHI_UNSPECIFIED:
            properties["chirality"] = str(chiral_tag)

        # 创建原子对象并添加到分子图
        atom = Atom(id="", symbol=rd_atom.GetSymbol(), properties=properties)
        atom_idx_to_id[atom_idx] = atom.id
        mol_graph.add_atom(atom)

    # 处理键
    for bond in mol.GetBonds():
        begin_atom = bond.GetBeginAtomIdx()
        end_atom = bond.GetEndAtomIdx()

        # 创建键ID (使用两个原子的索引)
        bond_id = f"{name}_b_{begin_atom}_{end_atom}"

        # 确定键类型
        bond_type = DICT_BOND_TYPE_REVERSE.get(bond.GetBondType(), BondType.SINGLE)

        # 收集键属性
        properties = {
            "aromatic": bond.GetIsAromatic(),
            "conjugated": bond.GetIsConjugated(),
            "in_ring": bond.IsInRing(),
            "bond_type_rdkit": str(bond.GetBondType()),
        }

        # 处理立体化学
        stereo = bond.GetStereo()
        if stereo != rdchem.BondStereo.STEREONONE:
            properties["stereo"] = str(stereo)

        # 创建键对象并添加到分子图
        bond = Bond(id="", source=atom_idx_to_id[begin_atom], target=atom_idx_to_id[end_atom],
                    type=bond_type, properties=properties)
        mol_graph.add_bond(bond)

    return mol_graph, atom_idx_to_id


def smiles_to_graph(smiles: str) -> Tuple[MolGraph, Dict[int, str]]:
    graph, atom_idx_to_id = mol_to_graph(Chem.MolFromSmiles(smiles), name=smiles, include_hs=False)
    return graph, atom_idx_to_id
