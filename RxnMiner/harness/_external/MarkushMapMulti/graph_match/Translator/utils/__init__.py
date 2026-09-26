from __future__ import annotations
import random
import json
import re
import base64
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum, unique
import tempfile
from typing import Sequence, Dict, Any, Optional, Tuple, List, Union, Literal

import numpy as np
import pandas as pd
from rdkit import Chem
from rdkit.Chem.Scaffolds import MurckoScaffold
from rdkit.Chem import rdRGroupDecomposition as rdRGD
from rdkit.Chem.Draw import rdMolDraw2D
from lxml import etree

# from nodejs import node


# String pattern for atom element, ',' exists in quadratic Bezier curve.
atom_path_pattern = re.compile(r'[MLQ,]\s\d+\.\d+\s\d+\.\d+')
bond_pattern = re.compile(r'bond-\d+')
atom_pattern = re.compile(r'atom-\d+')

# Load fonts.
# with open(str(Path(__file__).parent.parent/'fonts/settings.json')) as f:
#     font_settings = json.load(f)
# available_fonts = set(font_settings.keys())

# JS script for SVG rendering.
render_script = str(Path(__file__).parent.parent.joinpath('render.js').absolute())


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


def get_font_file(font: str) -> str:
    f = font_settings.get(font).get('fontfile')
    return str(Path(__file__).parent.parent / f'fonts/{f}')


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


def parse_bond_index(element: etree._Element) -> Tuple[int, int, int]:
    """Parse the @class attribute of a bond <path> element in SVG."""
    index = element.get('class', '')
    bonds = bond_pattern.findall(index)
    atoms = atom_pattern.findall(index)
    assert (
            len(bonds) == 1 and len(atoms) == 2 and atoms[0] != atoms[1]
    ), f'Invalid bond <path> @class={index}'

    return (
        int(bonds[0].split('-')[-1]),
        int(atoms[0].split('-')[-1]),
        int(atoms[1].split('-')[-1])
    )  # (bond idx, atom-0 idx, atom-1 idx)


def parse_atom_index(element: etree._Element) -> int:
    """Parse the @class attribute of an atom <path> or <text> element."""
    # Determine element type.
    is_text = element.tag == 'text'
    if is_text:
        index = element.get('id', '')
    else:
        index = element.get('class', '')
    atoms = atom_pattern.findall(index)
    if len(atoms) != 1:
        if is_text:
            raise RuntimeError(f'Invalid atom <text> @id={index}')
        else:
            raise RuntimeError(f'Invalid atom <path> @class={index}')
    return int(atoms[0].split('-')[-1])


def parse_bond_path(element: etree._Element) -> List[float]:
    """Parsing the @d attribute of a bond <path> element."""
    path = element.get('d', '')
    coords = re.compile(r'\d+\.\d+').findall(path)  # floating number
    return list(map(float, coords))


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


def merge_group(tgt_mol: Chem.rdchem.RWMol, src: str, attach_idx: int) -> None:
    """Merge a group of abbreviation into target molecule in place."""
    # print(f"input_mol_smi: {Chem.MolToSmiles(tgt_mol, canonical=False, isomericSmiles=True)}")
    # print(f"src: {src}")
    # print(f"attach_idx: {attach_idx}")
    dummy_atom = tgt_mol.GetAtomWithIdx(attach_idx)
    assert dummy_atom.GetSymbol() == '*', 'Non-dummy attachment point'
    src_mol = get_mol(src)

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


def get_atom_idx_with_map(mol, map_num):
    for atom in mol.GetAtoms():
        if atom.GetAtomMapNum() == map_num:
            return atom.GetIdx()

# def render(
#     svg: bytes,
#     font: str,
#     font_file: str,
#     write_to: Optional[str] = None
# ) -> Optional[bytes]:
#     """Wrapper for rendering SVG file."""
#     cmd = [
#         render_script,
#         '--font', font,
#         '--font-file', font_file,
#     ]
#     if write_to is not None:
#         cmd.extend(['--write-to', write_to])
#     p = node.run(cmd, input=svg.decode(), text=True, capture_output=True)
#     if p.returncode == 0:
#         return base64.b64decode(p.stdout)  # image bytes


def draw_mol(
        mol: Chem.rdchem.Mol,
        format: Literal['SVG', 'PNG'] = 'SVG',
        **draw_cfg
) -> Union[str, bytes]:
    if format == 'SVG':
        drawer = rdMolDraw2D.MolDraw2DSVG(width=-1, height=-1)
    elif format == 'PNG':
        drawer = rdMolDraw2D.MolDraw2DCairo(width=-1, height=-1)
    else:
        raise NotImplementedError

    dopts = rdMolDraw2D.MolDrawOptions()
    palette = draw_cfg.pop('palette', 'default')
    if palette == 'default':
        dopts.useDefaultAtomPalette()
    elif palette == 'avalon':
        dopts.useAvalonAtomPalette()
    elif palette == 'cdk':
        dopts.useCDKAtomPalette()
    elif palette == 'bw':
        dopts.useBWAtomPalette()
    else:
        raise NotImplementedError(f'Unsupported atom palette: {palette}')

    for k, v in draw_cfg.items():
        setattr(dopts, k, v)
    drawer.SetDrawOptions(dopts)

    # Start drawing.
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
    drawer.FinishDrawing()
    return drawer.GetDrawingText()  # type: str for SVG, bytes for PNG


class SaveTemporaryFile:
    def __init__(self, content: str, suffix: str = '.svg') -> None:
        self.file = tempfile.NamedTemporaryFile(mode='w', suffix=suffix, delete=False)
        self.file.write(content)
        self.file.flush()
        self.file.close()

    def __enter__(self) -> str:
        return self.file.name

    def __exit__(self, exc_type, exc_value, traceback):
        Path(self.file.name).unlink()  # delete temp file anyway


@unique
class Baseline(Enum):
    REGULAR = 'regular'
    SUP = 'sup'
    SUB = 'sub'

    @classmethod
    def lookup_by_value(cls, value: str) -> Baseline:
        return Baseline._value2member_map_.get(value)


@unique
class TextType(Enum):
    SYMBOL = 'symbol'
    SCRIPT = 'script'
    MULTIPLE = 'multiple'
    PRIME = 'prime'


@dataclass
class SVGText:
    text: str
    pos: Baseline
    type_: Optional[TextType] = None


class Index(int):
    def __new__(cls, value: int, **kwds):
        assert isinstance(value, int) and value >= 0
        return super().__new__(cls, value)


class AtomIndex(Index):
    key: int = 1

    def __hash__(self):
        return hash(('atom', int(self)))

    def __eq__(self, other):
        if isinstance(other, AtomIndex):
            return hash(self) == hash(other)
        return False


class RingIndex(Index):
    def __init__(self, value: int, virtual: bool = False) -> None:
        self._virtual = virtual
        self._key = 2_000_000 if self._virtual else 1_000_000

    def __hash__(self):
        return hash(('ring', int(self), self._virtual))

    def __eq__(self, other):
        if isinstance(other, RingIndex):
            return hash(self) == hash(other)
        return False

    @property
    def virtual(self) -> bool:
        return self._virtual

    @property
    def key(self) -> bool:
        return self._key