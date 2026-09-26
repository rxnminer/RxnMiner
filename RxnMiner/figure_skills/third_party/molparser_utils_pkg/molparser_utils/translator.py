# Translator\translator.py

import re
import logging
from pathlib import Path
from dataclasses import dataclass
from functools import reduce
from typing import Optional, List, Tuple, Union, Dict, Literal
from enum import Enum, unique

from rdkit import Chem, RDLogger
from rdkit.Chem.rdchem import Mol
from rdkit.Chem.Draw import rdMolDraw2D
from . import utils
# import sys
# sys.path.append('/mnt/vepfs/fs_users/caixc/MarkushMapMulti/Translator')
# import utils

# Removed 'Ar' (Argon), 'Ac', 'Pr', 'Cm', 'Cf', 'Es', 'Fm', 'Md', 'No', 'Lr'
PERIODIC_TABLE = {
    'H', 'He', 'Li', 'Be', 'B', 'C', 'N', 'O', 'F', 'Ne',
    'Na', 'Mg', 'Al', 'Si', 'P', 'S', 'Cl', 'K', 'Ca',
    'Sc', 'Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu', 'Zn',
    'Ga', 'Ge', 'As', 'Se', 'Br', 'Kr', 'Rb', 'Sr', 'Y', 'Zr',
    'Nb', 'Mo', 'Tc', 'Ru', 'Rh', 'Pd', 'Ag', 'Cd', 'In', 'Sn',
    'Sb', 'Te', 'I', 'Xe', 'Cs', 'Ba', 'La', 'Ce', 'Nd',
    'Pm', 'Sm', 'Eu', 'Gd', 'Tb', 'Dy', 'Ho', 'Er', 'Tm', 'Yb',
    'Lu', 'Hf', 'Ta', 'W', 'Re', 'Os', 'Ir', 'Pt', 'Au', 'Hg',
    'Tl', 'Pb', 'Bi', 'Po', 'At', 'Rn', 'Fr', 'Ra', 'Th',
    'Pa', 'U'
}


@unique
class TextType(Enum):
    SYMBOL = 'symbol'
    SCRIPT = 'script'
    MULTIPLE = 'multiple'
    PRIME = 'prime'


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
    def key(self) -> bool | int:
        return self._key


LONG_CHAR_ELEMENTS = (
    'Cl', 'Br', 'Si', 'Na', 'Al', 'Ca', 'Sn', 'As', 'Hg',
    'Fe', 'Zn', 'Cr', 'Se', 'Gd', 'Au', 'Li'
)
VISUALIZE_CFG = {
    'padding': 0.1,
    'additionalAtomLabelPadding': 0.05,
    'fixedFontSize': 12,
    'bondLineWidth': 1,
    'multipleBondOffset': 0.1,
    'dummiesAreAttachments': False,
    'addAtomIndices': False,
    'singleColourWedgeBonds': True,
    'legendFontSize': 9,
}

logger = logging.getLogger(__name__)


class Tokens:
    """A class of sepcial token definitions."""
    atom_start = '<a>'
    atom_end = '</a>'
    circ_start = '<c>'
    circ_end = '</c>'
    ring_start = '<r>'
    ring_end = '</r>'
    dummy = '<dum>'
    separator = '<sep>'


class Patterns:
    """A class of molecular expression patterns."""
    long_char_elements_pattern = re.compile(
        rf'{"|".join(LONG_CHAR_ELEMENTS) + "|."}')
    grp_content = re.compile(
        rf'(?P<{TextType.SYMBOL.value}>[A-Za-z0-9-\(\)]*)' +
        rf'(?P<{TextType.SCRIPT.value}>(\[\S+\])?)' +
        rf'(?P<{TextType.PRIME.value}>[\'\"]?)' +
        # $ for matching string ending
        #rf'(?P<{TextType.MULTIPLE.value}>(\?([a-z]|\d{1}|\d-\d)$)?)'
        rf'(?P<{TextType.MULTIPLE.value}>(\?([a-z]|\d+|\d-\d)$)?)'

    )
    grp_pattern = re.compile(
        rf'({Tokens.atom_start}|{Tokens.circ_start}|{Tokens.ring_start}|{Tokens.ring_start}{Tokens.circ_start})' +
        rf'(\d+:\S+?)' +
        rf'({Tokens.atom_end}|{Tokens.circ_end}|{Tokens.ring_end})'
    )
    trail_pattern = re.compile(
        r'(?P<groups>([^|]*)?)(?P<extension>(\|\S+\|)?$)')


@dataclass
class GroupDesc:
    id: Index
    symbol: Optional[str] = None
    script: Optional[str] = None
    prime: Optional[str] = None
    multiple: Optional[str] = None
    is_circle: bool = False
    is_dummy: bool = False

    @property
    def is_abbrev(self) -> bool:
        """Determine if the group describes abbreviation."""
        return (
            isinstance(self.id, AtomIndex)
            and (not self.is_dummy)
            and (not self.is_circle)
            and self.symbol in utils.get_abbrev_smi()
            and self.symbol != 'Ar'  # `Ar` might be a variable aromatic ring
            and self.script is None
            and self.prime is None
            and self.multiple is None
        )

    def __str__(self) -> str:
        if self.is_dummy:
            return '<dum>'
        expr = ''
        if self.is_circle:
            expr = 'c'
        if self.symbol is not None:
            expr += self.symbol
        if self.script is not None:
            expr = expr + '[' + self.script + ']'
        if self.prime is not None:
            expr += self.prime
        if self.multiple is not None:
            expr = expr + '?' + self.multiple
        return expr


@dataclass(frozen=True)
class TranslatedMolecule:
    smi: str
    groups: str
    caption: str
    markush: bool
    sru: bool


class Translator:
    """A helper class for tokenizing, parsing, and reconstruction etc.."""

    @classmethod
    def tokenize_smiles(cls, smi: str) -> List[str]:
        # Some elements may take up more than one character.
        # Ring id >= 10 may take up more then one character.
        parsed = re.findall(Patterns.long_char_elements_pattern, smi)
        tokens: List[str] = []
        i = 0
        while i < len(parsed):
            char = parsed[i]
            if char != '%':
                tokens.append(char)
                i += 1
            else:
                assert parsed[i + 1].isdigit() and parsed[i + 2].isdigit()
                tokens.append(char)
                tokens.append(parsed[i + 1] + parsed[i + 2])
                i += 3
        return tokens

    @classmethod
    def tokenize_symbol(cls, symbol: str) -> List[str]:
        # XXX Now we can simply parse symbol by uni-characters.
        return [char for char in symbol]

    @classmethod
    def tokenize_script(cls, script: str) -> List[str]:
        """Tokenize clean scripts without square brackets."""
        return re.findall(r'\d+|\D', script)

    @classmethod
    def tokenize_multiple(cls, multiple: str) -> List[str]:
        # XXX Now we can simply parse multiple by uni-characters.
        return [char for char in multiple]

    @classmethod
    def consolidate(cls, smi: str, grp_tokens: List[List[str]]) -> List[str]:
        """Combine tokens of SMILES and R groups."""
        tokens = cls.tokenize_smiles(rf'{smi}')
        tokens.append(Tokens.separator)
        if len(grp_tokens) > 0:
            tokens += reduce(lambda x, y: x + y, grp_tokens)
        return tokens

    @classmethod
    def tokenize_caption(
            cls,
            caption: str,
            error_msg: bool = True
    ) -> Optional[List[str]]:
        """Parse a full molecule caption (not necessarily Markush) into tokens."""
        parsed = cls.parse_caption(
            caption, return_mol=False, error_msg=error_msg
        )
        if parsed is None:
            return
        smi, groups, ext = parsed  # `groups` might be empty string
        tokens = cls.tokenize_smiles(smi)
        tokens.append(Tokens.separator)
        return tokens + cls.tokenize_groups(groups)

    @classmethod
    def tokenize_groups(cls, seq: str) -> List[str]:
        if seq == '':  # no R groups
            return []
        tokens = []
        for grp_start, grp_content, grp_end in re.findall(Patterns.grp_pattern, seq):
            if grp_start == f'{Tokens.ring_start}{Tokens.circ_start}':
                tokens += [Tokens.ring_start, Tokens.circ_start]
            else:
                tokens.append(grp_start)
            tokens += cls.tokenize_group(grp_content)
            tokens.append(grp_end)
        return tokens

    @classmethod
    def tokenize_group(cls, group: str) -> List[str]:
        """Tokenize content of an atom/ring substitute group."""
        items = group.split(':')
        assert len(items) == 2
        idx, content = items
        tokens = [idx, ':']
        if content == Tokens.dummy:
            return tokens + [Tokens.dummy]

        texts = cls.get_group_texts(content)
        tokens = []
        tokens += cls.tokenize_symbol(texts.get(TextType.SYMBOL, ''))
        script = texts.get(TextType.SCRIPT)
        if script is not None:
            tokens.append('[')
            tokens += cls.tokenize_script(script)
            tokens.append(']')
        tokens += [s for s in texts.get(TextType.PRIME, '')]
        multiple = texts.get(TextType.MULTIPLE)
        if multiple is not None:
            tokens.append('?')
            tokens += cls.tokenize_multiple(multiple)
        return tokens

    @classmethod
    def parse_caption(
            cls,
            caption: str,
            return_mol: bool = False,
            error_msg: bool = False
    ) -> Optional[Tuple[Union[Chem.rdchem.Mol, str], str, str]]:
        """Parse a complete molecule caption or prediction."""
        smi, *trailings = caption.split(Tokens.separator)
        if len(trailings) != 1:
            if error_msg:
                logger.warning(
                    f'{len(trailings)} `{Tokens.separator}` found in caption: {caption}'
                )
            return

        if error_msg:
            RDLogger.EnableLog('rdApp.*')
        mol = Chem.MolFromSmiles(smi)
        RDLogger.DisableLog('rdApp.*')
        if mol is None:
            if error_msg:
                logger.warning(f'Invalid SMILES: {smi}')
            return
        # Split groups description and extension.
        groups, ext = cls.parse_trailing(trailings[0])

        if return_mol:
            return mol, groups, ext
        return smi, groups, ext

    @classmethod
    def parse_trailing(cls, trailing: str):
        """Parse the trailing part in caption into R group and extension."""
        matched = re.match(Patterns.trail_pattern, trailing)
        if matched is None:
            return '', ''
        content = matched.groupdict()
        return content.get('groups', ''), content.get('extension', '')

    @classmethod
    def parse_extension(cls, ext: str) -> str:
        """Parse extension info in caption."""
        # TODO
        return ext.strip('|')

    @classmethod
    def parse_groups(cls, seq: str) -> List[GroupDesc]:
        """Parse R group texts from predictions."""
        if seq == '':
            return []
        descriptions = []
        for grp_start, grp_content, _ in re.findall(Patterns.grp_pattern, seq):
            parsed = cls.parse_group(grp_content)
            if parsed is None:
                continue
            idx, grp_text = parsed
            if grp_start == Tokens.atom_start:
                grp_desc = GroupDesc(id=AtomIndex(idx))
                if len(grp_text) == 0:
                    grp_desc.is_dummy = True
            elif grp_start == Tokens.circ_start:
                grp_desc = GroupDesc(id=AtomIndex(idx), is_circle=True)
            elif grp_start == f'{Tokens.ring_start}{Tokens.circ_start}':
                grp_desc = GroupDesc(id=RingIndex(idx, virtual=True))
            elif grp_start == Tokens.ring_start:
                grp_desc = GroupDesc(id=RingIndex(idx))
            else:
                continue
            grp_desc.symbol = grp_text.get(TextType.SYMBOL)
            grp_desc.script = grp_text.get(TextType.SCRIPT)
            grp_desc.prime = grp_text.get(TextType.PRIME)
            grp_desc.multiple = grp_text.get(TextType.MULTIPLE)
            descriptions.append(grp_desc)

        return descriptions

    @classmethod
    def parse_group(cls, group: str) -> Optional[Tuple[int, Dict[TextType, str]]]:
        """Get text of a R group."""
        items = group.split(':')
        if len(items) != 2:
            return
        idx, content = items
        if not idx.isdigit():
            return
        idx = int(idx)
        if content == Tokens.dummy:
            return idx, {}
        # Gotta make sure that not all text types are empty string.
        grp_text = cls.get_group_texts(content)
        if len(grp_text) == 0:
            return

        return idx, grp_text

    @classmethod
    def get_group_texts(cls, content: str) -> Dict[TextType, str]:
        """Helper for reorganizing texts in R group."""
        texts = {}
        # NOTE Unmatched items will be empty strings.
        matched = re.match(Patterns.grp_content, content).groupdict()
        for tt_value, text in matched.items():
            if len(text) == 0:
                continue
            if tt_value == TextType.SYMBOL.value:
                texts[TextType.SYMBOL] = text
            elif tt_value == TextType.SCRIPT.value:
                # Remove square brackets.
                assert text.startswith('[') and text.endswith(']')
                texts[TextType.SCRIPT] = text[1:-1]
            elif tt_value == TextType.PRIME.value:
                texts[TextType.PRIME] = text
            elif tt_value == TextType.MULTIPLE.value:
                assert text.startswith('?')
                texts[TextType.MULTIPLE] = text[1:]
        return texts

    @classmethod
    def reconstruct_mol(
            cls,
            mol: Union[Mol, str],
            groups: Optional[str] = None,
            write_to: Optional[str] = None,
            add_atom_indices: bool = False,
            drawer_type: Literal['SVG', 'PNG'] = 'SVG'
    ) -> Optional[Union[str, bytes]]:
        """Reconstruct a molecule based on prediction caption and draw."""
        if drawer_type not in ('SVG', 'PNG'):
            logger.error(f'Invalid drawer type `{drawer_type}`')
            return
        if isinstance(mol, Mol):
            assert groups is not None, 'Empty group information'
        elif isinstance(mol, str):
            assert groups is None, 'Excessive group information'
            parsed = cls.parse_caption(caption=mol, return_mol=True)
            if parsed is None:
                return
            mol, groups, ext = parsed
        else:
            raise TypeError(
                f'Invalid argument type: `{mol.__class__.__name__}`')

        grp_descriptions = cls.parse_groups(groups)

        # First check if this molecule has circle.
        circ_indices = [
            int(desc.id) for desc in grp_descriptions
            if desc.is_circle and desc.symbol is not None
        ]

        ring_captions = []
        for desc in grp_descriptions:
            i = int(desc.id)
            label = None
            if isinstance(desc.id, AtomIndex):
                if not 0 <= i < mol.GetNumAtoms():
                    continue
                atom = mol.GetAtomWithIdx(i)
                if atom.GetSymbol() == '*':
                    if desc.is_dummy:  # `DummyGroup` TODO
                        label = 'dum'
                    elif desc.symbol is None:
                        continue
                    elif desc.is_circle:  # `CircledRGroup` TODO
                        label = f'c{desc.symbol}'
                    else:
                        label = desc.symbol
                        if desc.script is not None:
                            label += f'<sub>{desc.script}</sub>'
                        if desc.prime is not None:
                            label += desc.prime
                        if desc.multiple is not None:  # `VariableGroup`
                            label = '(' + label + ')' + \
                                f'<sub>{desc.multiple}</sub>'
                elif atom.GetSymbol() in ('C', 'O'):  # `VariableGroup` on carbon or oxygen
                    if desc.symbol is None and desc.multiple is not None:
                        label = f'({atom.GetSymbol()})<sub>{desc.multiple}</sub>'
                if label is not None:
                    atom.SetProp('_displayLabel', label)
            elif isinstance(desc.id, RingIndex):  # TODO
                # Check if ring index is virtual.
                if (
                        (not desc.id.virtual) and (
                            not 0 <= i < mol.GetRingInfo().NumRings())
                        or (desc.id.virtual) and (i not in circ_indices)
                        or desc.symbol is None
                ):
                    continue
                label = desc.symbol
                if desc.script is not None:
                    label = label + f'[{desc.script}]'
                if desc.prime is not None:
                    label = label + desc.prime
                if desc.multiple is not None:
                    label = '(' + label + ')' + desc.multiple
                if desc.id.virtual:
                    legend = f'rv{i}: {label}'
                else:
                    legend = f'r{i}: {label}'
                ring_captions.append(legend)

        # Refine atom coordinates.
        mol.RemoveAllConformers()
        params = Chem.rdCoordGen.CoordGenParams()
        params.minimizerPrecision = params.sketcherBestPrecision
        Chem.rdCoordGen.AddCoords(mol, params)  # conformer generated with ID=0
        Chem.rdDepictor.StraightenDepiction(mol, 0)  # might rotate a little

        # Set drawing options.
        if write_to is not None:
            # NOTE Only PNG drawer can be used if image to be saved.
            drawer_type = 'PNG'
        if drawer_type == 'SVG':
            drawer = rdMolDraw2D.MolDraw2DSVG(width=-1, height=-1)
        else:
            drawer = rdMolDraw2D.MolDraw2DCairo(width=-1, height=-1)
        dopts = rdMolDraw2D.MolDrawOptions()
        dopts.useBWAtomPalette()  # black & white
        for k, v in VISUALIZE_CFG.items():
            setattr(dopts, k, v)
        dopts.addAtomIndices = add_atom_indices
        drawer.SetDrawOptions(dopts)
        rdMolDraw2D.PrepareAndDrawMolecule(
            drawer, mol, legend=' | '.join(ring_captions))
        drawer.FinishDrawing()

        if write_to is not None:
            # Directly save reconstructed image.
            Path(write_to).parent.mkdir(parents=True, exist_ok=True)
            drawer.WriteDrawingText(write_to)
            return

        if drawer_type == 'SVG':
            return drawer.GetDrawingText()  # str
        else:
            return drawer.GetDrawingText()  # base64 bytes for PNG

    @classmethod
    def refactor(
            cls,
            caption: str,
            error_msg: bool = False
    ) -> Optional[TranslatedMolecule]:
        """Attempt to put R group abbreviations back into recognized molecule.
        Also check if caption represents a Markush molecule.
        """
        if '<sep>' not in caption:
            return TranslatedMolecule(
                smi=caption,
                groups='',
                caption=caption,
                markush=False,
                sru=False
            )
        elif caption.split('<sep>')[-1] == '':
            return TranslatedMolecule(
                smi=caption.split('<sep>')[0],
                groups='',
                caption=caption.split('<sep>')[0],
                markush=False,
                sru=False
            )
        groups = caption.split('<sep>')[-1]
        smi = caption.split('<sep>')[0]

        try:
            mol = Chem.MolFromSmiles(smi)
            if mol is None:
                if error_msg:
                    logger.warning(f'Invalid SMILES: {smi}')
                return TranslatedMolecule(
                    smi=smi,
                    groups='',
                    caption=caption,
                    markush=len(groups) > 0,
                    sru=False
                )
            # Set original atom index as atom map number.
            for atom in mol.GetAtoms():
                atom.SetAtomMapNum(atom.GetIdx()+1)
            ring_info = mol.GetRingInfo().AtomRings()
            mapped_smiles = Chem.MolToSmiles(
                mol, canonical=False, isomericSmiles=True)
            mapped_caption = mapped_smiles + '<sep>' + groups

            parsed = cls.parse_caption(
                mapped_caption, return_mol=True, error_msg=error_msg)
            mol, groups, ext = parsed
            mol = Chem.RWMol(mol)

            mapnum2idx = {}
            for a in mol.GetAtoms():
                mn = a.GetAtomMapNum()
                if mn > 0:
                    mapnum2idx[mn] = a.GetIdx()

            to_remove = []
            is_markush = False  # flag for Markush structure
            is_sru = False  # flag for SRU structure

            if cls.parse_extension(ext) == 'Sg:n':
                is_sru = True

            abbrev_map = utils.get_abbrev_smi()
            abbrev_map_lower = {
                k.lower(): v for k, v in abbrev_map.items() if isinstance(k, str)}

            for desc in cls.parse_groups(groups):
                # atom map num = old_i + 1；现在的索引
                atom_idx = mapnum2idx.get(int(desc.id) + 1)
                if atom_idx is None:
                    is_markush = True
                    continue  # next group

                atom = mol.GetAtomWithIdx(atom_idx)

                # Only process dummy atoms for structural modification
                if atom.GetSymbol() != '*' and not desc.is_dummy:
                    is_markush = True
                    continue

                # Not symbol, ignore the group
                if not desc.symbol:
                    continue

                # Start R groups replace
                # ---------- ↓↓↓ step 0: 处理碳链重复的情况 ↓↓↓ ----------
                    
                # 存在碳链重复
                # if desc.multiple.isdigit() and int(desc.multiple)>0:
                #     desc.symbol

                # 存在碳链重复
                if (desc.symbol == 'CH2') or (desc.symbol == 'CH' and desc.script == '2'):
                    is_markush = utils.carbon_chain_repetition_process(
                        mol, atom_idx, desc, is_markush, error_msg=False)
                    continue  # 直接处理下一个 group，不再执行下方的查表逻辑

                else:  # 碳链不重复，继续执行
                    pass

                # ---------- ↑↑↑ step 0: 处理碳链重复的情况 ↑↑↑ ----------

                # ---------- ↓↓↓ step 1: 构建检索键（元素符号 + 角标脚本） ↓↓↓ ----------
                # Lookup Key (Symbol + Script) to find "NO2" from "NO" + "2"
                lookup_symbol = desc.symbol
                if desc.script:
                    lookup_symbol += desc.script

                # ---------- ↑↑↑ step 1: 构建检索键（元素符号 + 角标脚本） ↑↑↑ ----------

                # ---------- ↓↓↓ step 2. 缩写表内基团代入 ↓↓↓ ----------
                # 2. Priority Check: Abbreviation Mapping (exact first, then case-insensitive)

                if lookup_symbol == 'CN':  # 强制修正 CN 连接方向
                    src_smi = 'C(#N)'
                else:
                    src_smi = abbrev_map.get(lookup_symbol)  # 映射表中smiles
                    if src_smi is None:  # 查表失败，转小写二次尝试
                        src_smi = abbrev_map_lower.get(lookup_symbol.lower())

                if src_smi is not None:  # 查csv匹配到了smiles，开始代入
                    src_mol_check = Chem.MolFromSmiles(src_smi)

                    # Case 2a: Single Atom Abbreviation (e.g. Me->C) -> Atomic Mutation
                    if src_mol_check and src_mol_check.GetNumAtoms() == 1:
                        utils.alter_atom(atom, smiles=src_smi)
                        continue

                    # Case 2b: Multi-Atom Group -> Merge (Grafting)
                    # Must be terminal (Degree 1) to avoid valence issues
                    if atom.GetDegree() != 1:
                        if error_msg:
                            logger.warning(
                                f'Group `{lookup_symbol}` cannot be attached to atom {atom_idx} with degree {atom.GetDegree()}')
                        is_markush = True
                        continue

                    if any(not utils.is_single_bond(bond) for bond in atom.GetBonds()):
                        if error_msg:
                            logger.warning(
                                f'Group `{lookup_symbol}` must link to single bond')
                        is_markush = True
                        continue

                    try:
                        utils.merge_group(
                            tgt_mol=mol, src=src_smi, attach_idx=atom_idx)
                        to_remove.append(atom_idx)
                    except Exception as e:
                        if error_msg:
                            logger.warning(
                                f"Merge group failed: {e}")
                        is_markush = True
                    continue
                else:  # 查csv表匹配失败，继续执行
                    pass
                # ---------- ↑↑↑ step 2. 缩写表内基团代入 ↑↑↑ ----------

                # ---------- ↓↓↓ step 3. 元素周期表内原子代入 ↓↓↓ ----------
                # 3. Fallback: Periodic Table (Atomic Mutation)
                # Only if strictly matching an element symbol and not having extra modifiers

                # 确认是单原子
                if lookup_symbol in PERIODIC_TABLE and not desc.multiple:
                    try:
                        utils.alter_atom(atom, smiles=None,element=lookup_symbol)
                    except Exception as e:
                        if error_msg:
                            logger.warning(
                                f"Failed to mutate atom {atom_idx} to {lookup_symbol}: {e}")
                        is_markush = True
                    continue
                else:
                    pass
                # ---------- ↑↑↑ step 3. 元素周期表内原子代入 ↑↑↑ ----------

                # ---------- ↓↓↓ step 4. 既不是缩写也不是元素 -> 真 Markush ↓↓↓ ----------
                is_markush = True
                # ---------- ↑↑↑ step 4. 既不是缩写也不是元素 -> 真 Markush ↑↑↑ ----------

            # Cleanup replaced dummy atoms
            for i in sorted(to_remove, reverse=True):
                mol.RemoveAtom(i)

            try:
                Chem.SanitizeMol(mol)
            except Exception as e:
                if error_msg:
                    logger.error(f"Sanitize failed: {e}")
                return TranslatedMolecule(
                    smi=smi, groups=groups, caption=caption, markush=True, sru=is_sru
                )

            mol = Chem.MolFromSmiles(Chem.MolToSmiles(
                mol, canonical=True, isomericSmiles=True))

            # Re-map logic for Markush groups (unchanged logic)
            if is_markush:  # 各种带入之后，仍然有R基存在，需要重新映射
                new_groups = utils.remap_groups(mol, groups, ring_info)
            else:  # 没有R基，直接返回
                new_groups = ''
                for atom in mol.GetAtoms():
                    atom.SetAtomMapNum(0)

            new_smi = Chem.MolToSmiles(
                mol, canonical=True, isomericSmiles=True)

            return TranslatedMolecule(
                smi=new_smi,
                groups=new_groups,
                caption=new_smi + '<sep>' + new_groups if is_markush else new_smi,
                markush=is_markush,
                sru=is_sru
            )
        except Exception as e:
            if error_msg:
                logger.error(f'Error while refactoring SMILES: {smi}')
                logger.error(repr(e))
            return TranslatedMolecule(
                smi=smi,
                groups=groups,
                caption=caption,
                markush=len(groups) > 0,
                sru=False
            )
    @classmethod
    def markush_substitute(
        cls, 
        caption: str,
        substitution_map: dict,
        error_msg: bool = False
    ) -> Optional[TranslatedMolecule]:
        
        #string replace by substitution_map
        caption=utils.re_replace_smiles(caption,substitution_map)

        #run refactor
        return cls.refactor(caption, error_msg=error_msg)



if __name__ == "__main__":
    # esmiles = "*CCC<sep><a>0:NO</a>"
    # result = Translator.refactor(esmiles)

    smiles="C#CCO*S(=O)N<sep><a>4:CH2?15</a>"
    #substitution_map={"R[21.1]":"C"}
    #result=Translator.markush_substitute(esmiles,substitution_map)
    result=Translator.refactor(smiles)
    print(result)
