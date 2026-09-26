from __future__ import annotations
import random
import re
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence, Dict, Any, Optional, Tuple, List, Union

import numpy as np
import pandas as pd
from rdkit import Chem
from rdkit.Chem.Scaffolds import MurckoScaffold
from rdkit.Chem import rdRGroupDecomposition as rdRGD


# Load group abbreviations.
@dataclass(frozen=True)
class Abbreviation:
    name: str
    smi: str
    num_atoms: int  # exclude hydrogen
    symbol: str  # used for target
    label: str
    labelW: str

    def __str__(self) -> str:
        return f'{self.symbol} {self.smi} {self.label} {self.labelW}'


_abbrev_df = pd.read_csv(
    str(Path(__file__).parent / 'abbrevs.csv'), header=0, index_col=False
)
_abbrev_smi = dict()
_abbrev_dict = defaultdict(list)
for row in _abbrev_df.itertuples():
    _abbrev_dict[row.name].append(Abbreviation(
        name=row.name, smi=row.smiles, num_atoms=int(row.atoms),
        symbol=row.symbol, label=row.label, labelW=row.labelW
    ))
    _abbrev_smi[row.symbol] = row.smiles


def get_abbrev_smi() -> Dict[str, str]:
    return _abbrev_smi


def load_abbrevs_for_condense() -> str:
    """Load abbreviation options for RDKit condensing. Each label name will
    have only one candidate.
    """
    abbrevs = []
    for v in _abbrev_dict.values():
        if len(v) > 1:
            abbrevs.append(random.choice(v))
        else:
            abbrevs.append(v[0])
    abbrevs.sort(key=lambda x: x.num_atoms, reverse=True)
    return '\n'.join(str(a) for a in abbrevs)


def sample_one(options: Dict[Any, Any]):
    population, weights = zip(*options.items())
    return random.choices(population, weights, k=1)[0]


def roll(p: float) -> bool:
    return random.random() < p


def roll_trunc_normal(
        mu: float = 0.5,
        sigma: float = 0.15,
        min_val: float = 0.0,
        max_val: float = 1.0
) -> float:
    """Generate a random number following truncated normal distribution."""
    return np.clip(np.random.normal(mu, sigma), min_val, max_val)


def roll_poisson(lam: float = 1.0) -> float:
    return np.random.poisson(lam=lam)


def get_mol(smi: str) -> Chem.rdchem.Mol:
    mol = Chem.MolFromSmiles(smi)
    if mol is None:
        raise ValueError(f'Invalid SMILES: {smi}')
    return mol


def to_smiles(mol: Chem.rdchem.Mol, canonical: bool = False) -> str:
    return Chem.MolToSmiles(mol, rootedAtAtom=0, canonical=canonical)

def split_groups(groups: str) -> Tuple[Dict[int, Dict], Dict[int, Dict]]:
    pattern = r"<(?P<type>r|a)>(?P<content>.*?)</(?P=type)>"
    matches = re.finditer(pattern, groups)
    a_groups = {}
    r_groups = {}

    for m in matches:
        type_ = m.group("type")
        text = m.group("content")
        # 使用冒号分隔，提取ind和content
        if ":" in text:
            ind, content = text.split(":", 1)
        else:
            ind, content = "", text
        if type_ == "r":
            r_groups[int(ind)] = content
        else:
            a_groups[int(ind)] = content
    return a_groups, r_groups

def get_groups_str(a_groups: Dict[int, Dict], r_groups: Dict[int, Dict]=None) -> str:
    groups_str = ""
    # 按照key排序
    groups = dict(sorted(a_groups.items(), key=lambda x: x[0]))
    for k, v in groups.items():
        groups_str += f"<a>{k}:{v['content']}</a>"
    if r_groups is not None:
        groups = dict(sorted(r_groups.items(), key=lambda x: x[0]))
        for k, v in groups.items():
            groups_str += f"<r>{k}:{v['content']}</r>"
    return groups_str


def is_hydrogen(atom: Chem.rdchem.Atom) -> bool:
    """Determine if input atom is non-isotope hydrogen."""
    return atom.GetSymbol() == 'H' and atom.GetIsotope() == 0


def is_special_atom(atom: Chem.rdchem.Atom) -> bool:
    """Helper function for detecting special atoms."""
    if atom.GetIsotope() > 0:  # isotope
        return True
    if atom.GetFormalCharge() != 0:  # ion
        return True
    return False


def is_chiral_atom(atom: Chem.rdchem.Atom, tetrahedral: bool = False) -> bool:
    """Helper function for detecting chiral atoms."""
    if tetrahedral:
        return atom.GetChiralTag() in (
            Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CCW,
            Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CW
        )
    return atom.GetChiralTag() != Chem.rdchem.ChiralType.CHI_UNSPECIFIED


def is_stereo_bond(bond: Chem.rdchem.Bond) -> bool:
    """Helper function for detecting stereo bonds."""
    return bond.GetStereo() != Chem.rdchem.BondStereo.STEREONONE


def is_single_bond(bond: Chem.rdchem.Bond) -> bool:
    """Helper function for detecting single bonds."""
    return bond.GetBondType() == Chem.rdchem.BondType.SINGLE


def is_aromatic_ring(atoms: Sequence[Chem.rdchem.Atom]) -> bool:
    """Determine if a ring is aromatic with given atoms on ring."""
    return all(atom.GetIsAromatic() for atom in atoms)


def count_adjacent_outer_atoms(atom: Chem.rdchem.Atom) -> int:
    cnt = 0
    for nb in atom.GetNeighbors():
        if nb.GetDegree() == 1:
            cnt += 1
    return cnt


def replace_atom(
        rwmol: Chem.rdchem.RWMol,
        atom_idx: int,
        element: str = '*',
        preserve_props: bool = False
) -> None:
    """Wrap the operation of substituting with a new atom in place."""
    i = atom_idx
    aromatic = rwmol.GetAtomWithIdx(i).GetIsAromatic()
    rwmol.ReplaceAtom(i, Chem.rdchem.Atom(element), preserveProps=preserve_props)
    if aromatic:
        rwmol.GetAtomWithIdx(i).SetIsAromatic(True)


def calc_distance_matrix(mol: Chem.rdchem.Mol, fill_diagonal: bool = True) -> np.ndarray:
    """Calculate atom distance matrix."""
    mol.RemoveAllConformers()
    conf_id = mol.Compute2DCoords()
    dist_mat = Chem.rdmolops.Get3DDistanceMatrix(mol, conf_id)
    if fill_diagonal:
        np.fill_diagonal(dist_mat, 1_000_000_000)
    return dist_mat


def decompose_mol(mol: Union[str, Chem.rdchem.Mol]) -> Optional[Dict[str, Chem.rdchem.Mol]]:
    """Decompose input molecule into scaffold and side chains."""
    if isinstance(mol, str):
        mol = get_mol(smi=mol)
    scaffold = MurckoScaffold.GetScaffoldForMol(mol)
    # NOTE Input molecule may not have a scaffold (no ring).
    if scaffold.GetNumAtoms() == 0:
        return
    # We save scaffold and side chains as `Mol` instances.
    res, unmatched = rdRGD.RGroupDecompose([scaffold], [mol], asSmiles=False)
    if (
            len(res) != 1
            or len(unmatched) != 0
            or res[0].get('Core', '') == ''
    ):
        raise RuntimeError(
            f'Failed to decompose SMILES: {to_smiles(mol, canonical=True)}'
        )
    return res[0]


def find_attaching_atom_idx(mol: Chem.rdchem.Mol, R_label: str) -> int:
    """Find index of non-dummy atom that connects given R group in scaffold
    or side chain.
    """
    for atom in mol.GetAtoms():
        if atom.HasProp('dummyLabel') and atom.GetProp('dummyLabel') == R_label:
            assert atom.GetDegree() == 1
            return atom.GetNeighbors()[0].GetIdx()
    raise RuntimeError(f'Scaffold or side chain has no attaching point to {R_label}')


def get_side_chain_abbrev(mol: Chem.rdchem.Mol) -> Optional[str]:
    """Return abbreviation of decomposed side chain molecule if it exists."""
    # An abbreviated `Mol` has one super atom and one dummy attaching atom.
    if mol.GetNumAtoms() != 2:
        return
    for atom in mol.GetAtoms():
        if atom.GetSymbol() == '*' and atom.HasProp('atomLabel'):
            return atom.GetProp('atomLabel')
def alter_atom(atom:Chem.rdchem.Atom, smiles:None|str,element:None|str=None)-> None:
    """Alter the atom of the target molecule to the atom of the source molecule."""
    if smiles: #如果传入smiles，则从smiles中获取原子
        src_mol_check = Chem.MolFromSmiles(smiles)
        rep_atom = src_mol_check.GetAtomWithIdx(0)
        atom.SetAtomicNum(rep_atom.GetAtomicNum())
        atom.SetFormalCharge(rep_atom.GetFormalCharge())
    elif element: #如果传入element，则从元素周期表中获取原子
        atom.SetAtomicNum(Chem.GetPeriodicTable().GetAtomicNumber(element))
        atom.SetFormalCharge(0)
    else:
        raise ValueError("Either smiles or element must be provided")
                        
    # CRITICAL FIX: Reset Dummy Properties
    atom.SetNumExplicitHs(0)
    atom.SetNoImplicit(False)
    atom.SetIsAromatic(False) if atom.GetDegree() == 1 else None

def merge_group(tgt_mol: Chem.rdchem.RWMol, src: str, attach_idx: int) -> None:
    """Merge a group of abbreviation into target molecule in place."""
    # print(f"input_mol_smi: {Chem.MolToSmiles(tgt_mol, canonical=False, isomericSmiles=True)}")
    # print(f"src: {src}")
    # print(f"attach_idx: {attach_idx}")
    dummy_atom = tgt_mol.GetAtomWithIdx(attach_idx)
    assert dummy_atom.GetSymbol() == '*', 'Non-dummy attachment point'
    
    src_mol = get_mol(src)
    assert src_mol is not None, f'R_smiles:{src} is invalid'

    # Find index of the only neighbor of dummy attachment.
    neighbors = dummy_atom.GetNeighbors()
    assert len(neighbors) == 1, 'Attachment point must have exactly one neighbor'
    nb_idx = neighbors[0].GetIdx()  # real attachment point

    # Ensure single bond type.
    conn_bond = tgt_mol.GetBondBetweenAtoms(attach_idx, nb_idx)
    assert conn_bond.GetBondType() == Chem.BondType.SINGLE, (
        'Attachment point must link to single bond'
    )

    # Add atoms from source to target.
    # NOTE Keep track of the mapping between atom indices, as well as index of
    # the first atom in group in target molecule.
    idx_map = {}  # key: `g_mol` atom index, value: target mol atom index
    for atom in src_mol.GetAtoms():
        i = tgt_mol.AddAtom(atom)
        idx_map[atom.GetIdx()] = i
        # 新增：复制手性标签
        tgt_atom = tgt_mol.GetAtomWithIdx(i)
        tgt_atom.SetChiralTag(atom.GetChiralTag())
    # Add bonds from source to target.
    for bond in src_mol.GetBonds():
        begin_idx = bond.GetBeginAtomIdx()
        end_idx = bond.GetEndAtomIdx()
        j, k = idx_map[begin_idx], idx_map[end_idx]
        tgt_mol.AddBond(j, k, bond.GetBondType())
    # Attach first atom of source molecule to target molecule.
    # NOTE We cannot delete dummy atom now, as atom order must be preserved, instead
    # we delete them outside this funciton.
    tgt_mol.RemoveBond(attach_idx, nb_idx)  # must be done first
    tgt_mol.AddBond(nb_idx, idx_map[0], Chem.BondType.SINGLE)
    # print(f"output_mol_smi: {Chem.MolToSmiles(tgt_mol, canonical=False, isomericSmiles=True)}")

def remap_groups(mol: Chem.rdchem.Mol, groups: str,ring_info:tuple) -> str:
    """Remap the groups of the molecule."""
    old_ind2agroup, old_ind2rgroup = split_groups(groups)
    new_ind2agroup = {}
    ind_map = {}

    for atom in mol.GetAtoms():
        s = atom.GetSmarts()
        atom.SetAtomMapNum(0)
        if ':' not in s:
            continue
        else:
            try:
                old_ind = int(s.split(":")[1].replace(']', '')) - 1
            except:
                continue
        new_ind = atom.GetIdx()
        ind_map[old_ind] = new_ind
        group = old_ind2agroup.get(old_ind)
        if group is not None:
            new_ind2agroup[new_ind] = {'content': group, 'type': 'a'}
    new_ind2rgroup = {}
    if len(old_ind2rgroup) > 0:
        new_ring_info = mol.GetRingInfo().AtomRings()
        ring_map = {}
        new_ring_sets = [set(ring) for ring in new_ring_info]

        for orig_idx, orig_ring in enumerate(ring_info):
            mapped = {ind_map.get(atom, atom) for atom in orig_ring}
            best_match = None
            best_overlap = 0
            for new_idx, new_ring in enumerate(new_ring_sets):
                overlap = len(mapped.intersection(new_ring))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_match = new_idx
            if best_overlap == 0:
                ring_map[orig_idx] = None
            else:
                ring_map[orig_idx] = best_match
        
        for old_ind, group in old_ind2rgroup.items():
            if ring_map.get(old_ind) is not None:
                new_ind2rgroup[ring_map[old_ind]] = {'content': group, 'type': 'r'}
    new_groups = get_groups_str(new_ind2agroup, new_ind2rgroup)

    return new_groups


def carbon_chain_repetition_process(mol, atom_id, desc, is_markush, error_msg=False):

    atom = mol.GetAtomWithIdx(atom_id)
    # 获取重复次数，如果没有 ?n 则默认为 1
    count = 1
    if desc.multiple and desc.multiple.isdigit():
        count = int(desc.multiple)

    try:
        # 直接把当前的 * 变成 C
        atom.SetAtomicNum(6)
        atom.SetFormalCharge(0)
        atom.SetNumExplicitHs(0)
        atom.SetNoImplicit(False)
        atom.SetIsAromatic(False)        # 强制设为非芳香性
        atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)

        # 清洗化学键，比如把 Ar-* 之间的键强制改成单键
        for bond in atom.GetBonds():
            bond.SetBondType(Chem.BondType.SINGLE)
            bond.SetIsAromatic(False)

        # 扩链
        if count > 1:
            nbrs = atom.GetNeighbors()
            degree = len(nbrs)

            if degree == 2:
                nb_right = nbrs[1]
                right_idx = nb_right.GetIdx()

                mol.RemoveBond(atom_id, right_idx)

                current_end_idx = atom_id
                for _ in range(count - 1):
                    new_atom = Chem.Atom(6)
                    new_atom.SetIsAromatic(False)
                    new_idx = mol.AddAtom(new_atom)
                    mol.AddBond(current_end_idx, new_idx, Chem.BondType.SINGLE)
                    current_end_idx = new_idx

                mol.AddBond(current_end_idx, right_idx, Chem.BondType.SINGLE)

            # 末端 (Degree 1)
            elif degree == 1:
                current_end_idx = atom_id
                for _ in range(count - 1):
                    new_atom = Chem.Atom(6)
                    new_atom.SetIsAromatic(False)
                    new_idx = mol.AddAtom(new_atom)
                    mol.AddBond(current_end_idx, new_idx, Chem.BondType.SINGLE)
                    current_end_idx = new_idx

            else:
                # Degree > 2 暂不扩链
                if error_msg:
                    pass
                    #logger.warning(f"Complex chain expansion skipped for degree {degree}")
                is_markush = True

    except Exception as e:
        if error_msg:
            #logger.warning(f"Chain expansion failed for atom {atom_id}: {e}")
            is_markush = True

    return is_markush

def re_replace_smiles(esmiles:str, substitution_map: dict)->str:
    for k, v in substitution_map.items():
        pattern = rf"(<a>\d+:){re.escape(k)}(</a>)"
        if not re.search(pattern, esmiles):
            print(f"Key `{k}` not matched in groups, it will not be substituted")
            continue
        esmiles = re.sub(pattern, rf"\1{v}\2", esmiles)
    return esmiles

def get_last_dummy_atom_idx(mol: Chem.Mol) -> int | None:
    """
    找到分子中最后一个虚拟原子（*）的原子索引（idx）
    Args:
        mol: RDKit分子对象（需包含虚拟原子*）
    Returns:
        int: 最后一个*的idx；若没有*，返回None
    """
    if mol is None:
        print("错误：分子对象为None")
        return None
    
    # 遍历所有原子，收集虚拟原子的idx（两种判定方式，确保兼容）
    dummy_atom_idxs = []
    for atom in mol.GetAtoms():
        # 判定条件1：原子符号是*（直接标记的虚拟原子）
        # 判定条件2：原子序数是0（RDKit标准虚拟原子）
        if atom.GetSymbol() == "*" or atom.GetAtomicNum() == 0:
            dummy_atom_idxs.append(atom.GetIdx())
    
    # 取最后一个*的idx
    if dummy_atom_idxs:
        return dummy_atom_idxs[-1]
    else:
        print("提示：分子中未找到虚拟原子*")
        return None
def merge_repeat_groups(smiles,multiple):

    base_mol=Chem.MolFromSmiles(smiles)
    base_mol = Chem.RWMol(base_mol)
    for repeat_id in range(multiple-1):
        dummy_id=get_last_dummy_atom_idx(base_mol)
        merge_group(base_mol,smiles,dummy_id)
        base_mol.RemoveAtom(dummy_id)
    return base_mol
