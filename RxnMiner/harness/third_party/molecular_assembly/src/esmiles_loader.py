"""DP eSMILES loader — Python alignment with Indigo ``esmiles_loader.cpp`` / ``rdkit_esmiles.cpp``.

Format::

    {SMILES body}<sep>{extension tags}

Supported tags (subset used by Synfacts / markush decompose):

- ``<a>{pos}:{label}</a>`` — R-group / attachment label at SMILES atom position
- ``<c>{pos}:{tag}?</c>`` — Markush ring pseudo atom (stored as metadata)
- ``<d>{pos}:<dum></d>`` — remove dummy * at position, mark neighbor as attachment
- ``<v>{pos}:…</v>`` — virtual arc metadata (recorded, not fully expanded in RDKit path)
- ``<g>…</g>`` / ``|Sg:…|`` — S-group payload (recorded)

Reference C++: ``core/indigo-core/molecule/src/esmiles_loader.cpp``.
SMILES parse params mirror ``rdkit_esmiles.cpp`` (``removeHs=False``, sanitize with dative fallback).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rdkit import Chem

from esmiles_contract import AttachmentContract, build_attachment_contract
from r_groups import RTagSlot, normalize_r_tags

ESMILES_SEPARATOR = "<sep>"
ESMILES_DUMMY_TOKEN = "<dum>"

_TAG_ACDV = re.compile(r"<([acdv])>(.*?)</\1>", re.DOTALL)
_CHILD_OPEN = "<r>"
_CHILD_CLOSE = "</r>"
_SG_G = re.compile(r"<g>(.*?)</g>", re.DOTALL)
_SG_PIPE = re.compile(r"\|Sg:[^|]+\|")


@dataclass(frozen=True)
class EsmilesTag:
    kind: str
    position: int
    value: str
    raw_value: str = ""


@dataclass
class EsmilesSGroupTag:
    value: str
    connection_points: list[tuple[int, int]] = field(default_factory=list)
    is_whole_molecule: bool = False


@dataclass
class EsmilesLoadResult:
    raw: str
    smiles: str
    info: str
    mol: Chem.Mol
    atom_by_position: list[int]
    a_tags: list[EsmilesTag]
    c_tags: list[EsmilesTag]
    d_tags: list[EsmilesTag]
    v_tags: list[EsmilesTag]
    sgroup_tags: list[EsmilesSGroupTag]
    anchor_slots: list[RTagSlot]
    attachment_contract: AttachmentContract | None = None
    messages: list[str] = field(default_factory=list)
    backend: str = "rdkit"


def strip_nul(s: str) -> str:
    while s and s[-1] == "\0":
        s = s[:-1]
    return s


def split_esmiles(raw: str) -> tuple[str, str]:
    """Split body and extension like C++ ``loadEsmilesIntoMolecule``."""
    text = strip_nul(raw.strip())
    sep = text.find(ESMILES_SEPARATOR)
    if sep == -1:
        return text, ""
    return text[:sep].strip(), text[sep + len(ESMILES_SEPARATOR) :].strip()


def strip_sgroup_suffix(value: str) -> str:
    pos = value.find("?")
    return value if pos == -1 else value[:pos]


def parse_sgroup_suffixes(value: str) -> list[str]:
    out: list[str] = []
    pos = value.find("?")
    while pos != -1:
        nxt = value.find("?", pos + 1)
        chunk = value[pos + 1 :] if nxt == -1 else value[pos + 1 : nxt]
        if chunk:
            out.append(chunk)
        pos = nxt
    return out


def _find_matching_child_close(info: str, open_pos: int) -> int:
    scan = open_pos + len(_CHILD_OPEN)
    depth = 1
    while scan < len(info) and depth > 0:
        nxt_open = info.find(_CHILD_OPEN, scan)
        nxt_close = info.find(_CHILD_CLOSE, scan)
        if nxt_close == -1:
            return -1
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            scan = nxt_open + len(_CHILD_OPEN)
            continue
        depth -= 1
        if depth == 0:
            return nxt_close
        scan = nxt_close + len(_CHILD_CLOSE)
    return -1


def parse_acdv_tags(info: str) -> list[EsmilesTag]:
    """Parse ``<a>``, ``<c>``, ``<d>``, ``<v>`` tags; skip nested ``<r>`` blocks."""
    tags: list[EsmilesTag] = []
    pos = 0
    while pos < len(info):
        if info.startswith(_CHILD_OPEN, pos):
            close = _find_matching_child_close(info, pos)
            if close == -1:
                break
            pos = close + len(_CHILD_CLOSE)
            continue

        m = _TAG_ACDV.match(info, pos)
        if not m:
            pos += 1
            continue

        content = m.group(2)
        colon = content.find(":")
        if colon == -1:
            pos = m.end()
            continue
        try:
            position = int(content[:colon].strip())
        except ValueError:
            pos = m.end()
            continue
        value = content[colon + 1 :]
        tags.append(
            EsmilesTag(
                kind=m.group(1),
                position=position,
                value=strip_sgroup_suffix(value),
                raw_value=value,
            )
        )
        pos = m.end()
    return tags


def parse_sgroup_tags(info: str) -> list[EsmilesSGroupTag]:
    tags: list[EsmilesSGroupTag] = []
    pos = 0
    while pos < len(info):
        if info.startswith(_CHILD_OPEN, pos):
            close = _find_matching_child_close(info, pos)
            if close == -1:
                break
            pos = close + len(_CHILD_CLOSE)
            continue

        gm = _SG_G.match(info, pos)
        if gm:
            tags.append(EsmilesSGroupTag(value=gm.group(1).strip()))
            pos = gm.end()
            continue

        pm = _SG_PIPE.match(info, pos)
        if pm:
            tags.append(EsmilesSGroupTag(value=pm.group(0), is_whole_molecule=True))
            pos = pm.end()
            continue
        pos += 1
    return tags


def build_atom_by_position(mol: Chem.Mol, smiles: str) -> list[int]:
    """SMILES output position → RDKit atom idx (align DP ``rootedAtAtom=0``)."""
    order_prop: Any = None
    try:
        if mol.HasProp("_smilesAtomOutputOrder"):
            order_prop = mol.GetProp("_smilesAtomOutputOrder")
    except Exception:
        order_prop = None

    if order_prop:
        try:
            indices = [int(x) for x in re.findall(r"-?\d+", str(order_prop))]
            if len(indices) == mol.GetNumAtoms():
                return indices
        except Exception:
            pass

    try:
        rw = Chem.Mol(mol)
        params = Chem.SmilesWriteParams()
        params.rootedAtAtom = 0 if rw.GetNumAtoms() > 0 else -1
        params.canonical = False
        params.doIsomericSmiles = True
        Chem.MolToSmiles(rw, params)
        if rw.HasProp("_smilesAtomOutputOrder"):
            indices = [
                    int(x) for x in re.findall(r"-?\d+", rw.GetProp("_smilesAtomOutputOrder"))
            ]
            if len(indices) == mol.GetNumAtoms():
                return indices
    except (AttributeError, TypeError):
        try:
            rw = Chem.Mol(mol)
            smi = Chem.MolToSmiles(rw, canonical=False, isomericSmiles=True)
            if smi and rw.HasProp("_smilesAtomOutputOrder"):
                indices = [
                    int(x)
                    for x in re.findall(r"-?\d+", rw.GetProp("_smilesAtomOutputOrder"))
                ]
                if len(indices) == mol.GetNumAtoms():
                    return indices
        except Exception:
            pass
    except Exception:
        pass

    _ = smiles
    return list(range(mol.GetNumAtoms()))


def atom_id_by_position(atom_by_position: list[int], position: int) -> int:
    if position < 0 or position >= len(atom_by_position):
        return -1
    return atom_by_position[position]


def _load_smiles_rdkit(smiles: str, messages: list[str]) -> Chem.Mol | None:
    """Mirror ``rdkit_esmiles::getMolWithDativeFallback``."""
    smiles = smiles.strip()
    if not smiles:
        return None

    def _try(sanitize: bool) -> Chem.Mol | None:
        try:
            params = Chem.SmilesParserParams()
            params.removeHs = False
            params.sanitize = sanitize
            return Chem.MolFromSmiles(smiles, params)
        except TypeError:
            return Chem.MolFromSmiles(smiles, sanitize=sanitize)

    mol = _try(True)
    if mol is None and "->" in smiles:
        mol = _try(False)
        if mol is not None:
            messages.append("SMILES 以非 sanitize 模式加载（含配位键 ``->``）。")
    if mol is None:
        mol = Chem.MolFromSmiles(smiles, sanitize=False)
    return mol


def _try_indigo_mol(raw: str) -> Chem.Mol | None:
    try:
        from indigo import Indigo  # type: ignore
    except ImportError:
        return None
    try:
        indigo = Indigo()
        obj = indigo.loadMolecule(raw)
        block = obj.molfile()
        return Chem.MolFromMolBlock(block, sanitize=False, removeHs=False)
    except Exception:
        return None


def load_smiles_body(smiles: str, *, messages: list[str] | None = None) -> tuple[Chem.Mol | None, list[int], str]:
    """Load SMILES body only; returns (mol, atom_by_position, backend)."""
    log = messages if messages is not None else []
    mol = _load_smiles_rdkit(smiles, log)
    backend = "rdkit"
    if mol is None:
        return None, [], backend
    order = build_atom_by_position(mol, smiles)
    return mol, order, backend


def _apply_dummy_tags(
    rw: Chem.RWMol,
    d_tags: list[EsmilesTag],
    atom_by_position: list[int],
    atoms_to_remove: set[int],
    messages: list[str],
) -> None:
    for tag in d_tags:
        if tag.value != ESMILES_DUMMY_TOKEN:
            continue
        atom_idx = atom_id_by_position(atom_by_position, tag.position)
        if atom_idx < 0 or atom_idx >= rw.GetNumAtoms():
            messages.append(f"<d> 位置 {tag.position} 无效，已跳过。")
            continue
        atom = rw.GetAtomWithIdx(atom_idx)
        if atom.GetAtomicNum() != 0:
            messages.append(f"<d> 位置 {tag.position} 非 dummy，已跳过。")
            continue
        nbrs = list(atom.GetNeighbors())
        if len(nbrs) != 1:
            messages.append(f"<d> 位置 {tag.position} dummy 度数≠1，已跳过。")
            continue
        atoms_to_remove.add(atom_idx)


def _remove_atoms(rw: Chem.RWMol, atoms_to_remove: set[int]) -> Chem.Mol:
    for idx in sorted(atoms_to_remove, reverse=True):
        rw.RemoveAtom(idx)
    return rw.GetMol()


def _annotate_tag_metadata(mol: Chem.Mol, tags: list[EsmilesTag], prefix: str) -> Chem.Mol:
    rw = Chem.RWMol(mol)
    for i, tag in enumerate(tags):
        rw.SetProp(f"_esmiles_{prefix}_{i}", f"{tag.position}:{tag.value}")
    return rw.GetMol()


def load_esmiles(
    raw: str,
    *,
    apply_dummy: bool = True,
    prefer_indigo: bool = True,
) -> EsmilesLoadResult | None:
    """Full eSMILES load aligned with DP tag order (main SMILES → tags → dummy removal)."""
    text = strip_nul(raw.strip())
    if not text:
        return None

    smiles, info = split_esmiles(text)
    messages: list[str] = []
    backend = "rdkit"
    mol: Chem.Mol | None = None
    atom_by_position: list[int] = []

    if prefer_indigo and info:
        indigo_mol = _try_indigo_mol(text)
        if indigo_mol is not None:
            mol = indigo_mol
            backend = "indigo"
            messages.append("使用 Indigo 加载 eSMILES（与 DP C++ 路径一致）。")

    if mol is None:
        mol, atom_by_position, backend = load_smiles_body(smiles, messages=messages)
        if mol is None:
            return None
    else:
        atom_by_position = build_atom_by_position(mol, smiles)

    acdv = parse_acdv_tags(info) if info else []
    a_tags = [t for t in acdv if t.kind == "a"]
    c_tags = [t for t in acdv if t.kind == "c"]
    d_tags = [t for t in acdv if t.kind == "d"]
    v_tags = [t for t in acdv if t.kind == "v"]
    sgroup_tags = parse_sgroup_tags(info) if info else []

    anchor_slots = normalize_r_tags(
        "".join(f"<a>{t.position}:{t.raw_value or t.value}</a>" for t in a_tags)
        if a_tags
        else (info if "<a>" in info else "")
    )

    atoms_to_remove: set[int] = set()
    if apply_dummy and d_tags:
        rw = Chem.RWMol(mol)
        _apply_dummy_tags(rw, d_tags, atom_by_position, atoms_to_remove, messages)
        if atoms_to_remove:
            n_before = rw.GetNumAtoms()
            old_to_new: dict[int, int] = {}
            new_idx = 0
            for old in range(n_before):
                if old not in atoms_to_remove:
                    old_to_new[old] = new_idx
                    new_idx += 1
            mol = _remove_atoms(rw, atoms_to_remove)
            atom_by_position = [
                old_to_new.get(atom_by_position[pos], -1)
                for pos in range(len(atom_by_position))
            ]

    anchor_slots = resolve_anchor_dummy_indices(mol, anchor_slots, atom_by_position)

    if c_tags or v_tags or sgroup_tags:
        mol = _annotate_tag_metadata(mol, c_tags, "c")
        mol = _annotate_tag_metadata(mol, v_tags, "v")

    contract = build_attachment_contract(info, anchor_slots, v_tags)
    messages.extend(contract.parse_warnings)

    return EsmilesLoadResult(
        raw=text,
        smiles=smiles,
        info=info,
        mol=mol,
        atom_by_position=atom_by_position,
        a_tags=a_tags,
        c_tags=c_tags,
        d_tags=d_tags,
        v_tags=v_tags,
        sgroup_tags=sgroup_tags,
        anchor_slots=anchor_slots,
        attachment_contract=contract,
        messages=messages,
        backend=backend,
    )


def load_esmiles_line(line: str, **kwargs: Any) -> EsmilesLoadResult | None:
    return load_esmiles(line, **kwargs)


def parse_fragment_line(line: str) -> tuple[str, str] | None:
    """Backward-compatible split; prefer :func:`split_esmiles`."""
    line = line.strip()
    if not line or ESMILES_SEPARATOR not in line:
        return None
    smi, info = split_esmiles(line)
    return smi, info


def resolve_anchor_dummy_indices(
    mol: Chem.Mol,
    anchor_slots: list[RTagSlot],
    atom_by_position: list[int],
) -> list[RTagSlot]:
    """Map tag SMILES positions → current RDKit atom indices after load."""
    remapped: list[RTagSlot] = []
    for slot in anchor_slots:
        pos = slot.dummy_idx
        idx = atom_id_by_position(atom_by_position, pos)
        if (
            idx >= 0
            and idx < mol.GetNumAtoms()
            and mol.GetAtomWithIdx(idx).GetAtomicNum() != 0
            and pos < mol.GetNumAtoms()
            and mol.GetAtomWithIdx(pos).GetAtomicNum() == 0
        ):
            idx = pos
        if idx < 0:
            idx = pos if pos < mol.GetNumAtoms() else -1
        if idx < 0:
            continue
        remapped.append(
            RTagSlot(
                dummy_idx=idx,
                source_label=slot.source_label,
                internal_id=slot.internal_id,
                display_label=slot.display_label,
                pair_key=slot.pair_key,
                occurrence=slot.occurrence,
                smiles_position=slot.smiles_position,
            )
        )
    return remapped


def validate_esmiles_line(line: str, *, prefer_indigo: bool = False) -> list[str]:
    """Return issue strings; empty list means structurally OK for ``<a>`` tags."""
    text = (line or "").strip()
    if not text:
        return ["empty"]
    if ESMILES_SEPARATOR not in text:
        return ["missing <sep>"]

    loaded = load_esmiles(text, prefer_indigo=prefer_indigo)
    if loaded is None:
        return ["SMILES body parse failed"]

    issues: list[str] = []
    mol = loaded.mol
    for tag in loaded.a_tags:
        pos = tag.position
        if pos < 0 or pos >= len(loaded.atom_by_position):
            issues.append(f"<a> pos={pos} out of range (n={mol.GetNumAtoms()})")
            continue
        idx = atom_id_by_position(loaded.atom_by_position, pos)
        if idx < 0:
            issues.append(f"<a> pos={pos} unmapped")
            continue
        atom = mol.GetAtomWithIdx(idx)
        if atom.GetAtomicNum() != 0:
            issues.append(
                f"<a> pos={pos} idx={idx} not dummy (*), symbol={atom.GetSymbol()}"
            )

    if loaded.a_tags and not loaded.anchor_slots:
        issues.append("<a> tags present but no valid anchor slots")

    return issues
