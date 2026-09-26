"""Expand known abbreviations in template E-SMILES before Step0 load/draw.

Concrete ``<a>`` labels (Ph, NPhth, …) are grafted via ``chemistry.graft`` →
``assemble_fragments`` / ``build_complete_molecule`` (same join as R-pair assembly).
"""
from __future__ import annotations

import re

from rdkit import Chem
from chemistry.r_groups import _R_BRACKET, _R_PLAIN, parse_tag_string
from translator import Translator
from translator import _utils as translator_utils

from .encode import encode_substituent_esmiles
from .loader import split_esmiles

_A_TAG = re.compile(r"<a>\d+:[^<]*</a>")
_PIPE_EXT = re.compile(r"(\|[^|]+\|)$")
_R_DIGIT = re.compile(r"^R\d+(['′″‴]|#\d+)?$", re.IGNORECASE)
_OPEN_MARKUSH_LABELS = {"ar", "alk"}


def _connectivity_smiles(smiles: str) -> str | None:
    mol = Chem.MolFromSmiles((smiles or "").strip())
    if mol is None:
        return None
    return Chem.MolToSmiles(mol, isomericSmiles=False)


def _preserve_stereo_body_if_refactor_stripped(original_body: str, new_body: str) -> str:
    """``Translator.refactor`` may drop ``@`` on Markush * lines while keeping topology."""
    orig = (original_body or "").strip()
    new = (new_body or "").strip()
    if not orig or not new or orig == new:
        return new
    if ("@" not in orig and "@@" not in orig) or "@" in new or "@@" in new:
        return new
    if "*" not in orig:
        return new
    key_orig = _connectivity_smiles(orig)
    key_new = _connectivity_smiles(new)
    if key_orig and key_new and key_orig == key_new:
        return orig
    return new


def _is_open_markush_label(label: str) -> bool:
    lab = (label or "").strip()
    if not lab or lab == "<dum>":
        return True
    if lab.lower() in _OPEN_MARKUSH_LABELS:
        return True
    if _R_BRACKET.match(lab) or _R_PLAIN.match(lab) or _R_DIGIT.match(lab):
        return True
    return False


def _lookup_abbrev_smiles(label: str) -> str:
    raw = (label or "").strip()
    if not raw or _is_open_markush_label(raw):
        return ""
    # Synfacts / OCSR: ``CN`` = nitrile. Never use isocyanide ``[N+]#[C-]``.
    if raw == "CN":
        return "C#N"
    abbrev_map = translator_utils.get_abbrev_smi()
    smi = abbrev_map.get(raw)
    if smi:
        return smi
    low = raw.lower()
    for key, value in abbrev_map.items():
        if isinstance(key, str) and key.lower() == low:
            return value
    return ""


def lookup_abbreviation_smiles(label: str) -> str:
    """Return the concrete SMILES for a known, non-Markush abbreviation."""
    return _lookup_abbrev_smiles(label)


def _atom_idx_for_smiles_position(
    mol: Chem.Mol,
    smiles: str,
    position: int,
) -> int:
    from .loader import atom_id_by_position, build_atom_by_position

    atom_by_position = build_atom_by_position(mol, smiles)
    atom_idx = atom_id_by_position(atom_by_position, position)
    if atom_idx < 0 and 0 <= position < mol.GetNumAtoms():
        atom_idx = position
    return atom_idx


def substitute_known_abbrev_slots_with_mol(
    scaffold_line: str,
    messages: list[str] | None = None,
    assembly_trace: list[dict[str, object]] | None = None,
) -> tuple[str, Chem.Mol | None]:
    """Graft concrete abbreviation slots and retain the assembled in-memory Mol.

    Same chemical op as ``assemble_fragments``: pair scaffold ``*`` with a
    star-prefixed group fragment. Open Markush labels (``Alk``, ``R[1]``, …)
    stay as dummies and are re-encoded after the join.  The returned Mol is
    the exact post-graft, pre-``encode_substituent_esmiles`` calculation state.

    Unlike the old in-place ``BondDir`` copy, stereo is finalized by assembly
    (``ChiralTag`` transfer via AtomMapNum).
    """
    from chemistry.graft import (
        assemble_labeled_fragments,
        placeholders_as_star_dummies,
        remaining_dummy_rlabels,
        star_body_from_group_smiles,
    )

    log = messages if messages is not None else []
    line = (scaffold_line or "").strip()
    if not line or "<sep>" not in line:
        return scaffold_line, None

    body, extension = split_esmiles(line)
    tags = parse_tag_string(extension)
    if not body or not tags:
        return scaffold_line, None

    body_mol = Chem.MolFromSmiles(body)
    if body_mol is None:
        return scaffold_line, None

    # (smiles_position, pair_label_or_open, abbrev_smi_or_empty)
    scaffold_tags: list[tuple[int, str]] = []
    fragment_lines: list[str] = []
    grafted: list[str] = []
    abbrev_i = 0

    for position, label in tags:
        atom_idx = _atom_idx_for_smiles_position(body_mol, body, position)
        if atom_idx < 0 or atom_idx >= body_mol.GetNumAtoms():
            continue
        atom = body_mol.GetAtomWithIdx(atom_idx)
        if atom.GetAtomicNum() != 0:
            continue
        abbrev_smi = _lookup_abbrev_smiles(label)
        if (
            not abbrev_smi
            or atom.GetDegree() != 1
            or not atom.GetBonds()
            or atom.GetBonds()[0].GetBondType() != Chem.BondType.SINGLE
        ):
            scaffold_tags.append((position, label))
            continue
        try:
            star_body = star_body_from_group_smiles(abbrev_smi)
        except Exception:
            scaffold_tags.append((position, label))
            continue
        pair = f"__abbrev_{abbrev_i}"
        abbrev_i += 1
        scaffold_tags.append((position, pair))
        fragment_lines.append(f"{star_body}<sep><a>0:{pair}</a>")
        grafted.append(label)

    if not fragment_lines:
        return scaffold_line, None

    scaffold_tags.sort(key=lambda x: x[0])
    tag_str = "".join(f"<a>{pos}:{lbl}</a>" for pos, lbl in scaffold_tags)
    lines = [f"{body}<sep>{tag_str}", *fragment_lines]
    asm_notes: list[str] = []
    mol = assemble_labeled_fragments(lines, asm_notes, trace=assembly_trace)
    if mol is None:
        return scaffold_line, None

    mol = placeholders_as_star_dummies(mol)
    try:
        Chem.SanitizeMol(mol)
        Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
    except Exception:
        return scaffold_line, None

    out = encode_substituent_esmiles(mol, remaining_dummy_rlabels(mol))
    if grafted:
        log.append("模板缩写已局部代入：" + ", ".join(sorted(set(grafted))))
    return out, mol


def substitute_known_abbrev_slots(
    scaffold_line: str,
    messages: list[str] | None = None,
) -> str:
    """Graft known abbreviation slots, returning only the exported eSMILES."""
    out, _mol = substitute_known_abbrev_slots_with_mol(scaffold_line, messages)
    return out


def expand_template_abbreviations(
    scaffold_line: str,
    messages: list[str] | None = None,
) -> str:
    """Expand template ``<a>`` tags: CH2?k, known abbrev graft, then Translator fallback.

    Order matters: numeric ``CH2?k`` runs **before** abbrev graft so coexistence
    (e.g. ``CH2?9`` + ``Bs``) is not skipped when graft succeeds. Abstract
    ``CH2?n`` is kept and must not fall through to broken ``refactor`` multiples.
    """
    log = messages if messages is not None else []
    line = scaffold_line.strip()
    if not line or "<sep>" not in line:
        return scaffold_line

    from .methylene import expand_numeric_ch2_in_esmiles, line_has_abstract_ch2

    after_ch2 = expand_numeric_ch2_in_esmiles(line, log)
    local = substitute_known_abbrev_slots(after_ch2, messages)
    if local != after_ch2:
        return local
    if after_ch2 != line:
        return after_ch2
    # Abstract CH2?n etc.: do not let Translator.refactor silently collapse them.
    if line_has_abstract_ch2(after_ch2):
        return after_ch2

    body, extension = split_esmiles(line)
    a_tags = "".join(_A_TAG.findall(extension))
    if not a_tags:
        return scaffold_line

    rest = _A_TAG.sub("", extension)
    pipe_ext = ""
    pipe_match = _PIPE_EXT.search(rest)
    if pipe_match:
        pipe_ext = pipe_match.group(1)
        rest = rest[: pipe_match.start()]

    refactor_input = f"{body}<sep>{a_tags}"
    result = Translator.refactor(refactor_input, error_msg=False)
    if result is None:
        log.append("模板缩写展开失败，保留原始骨架行。")
        return scaffold_line

    if result.caption == refactor_input:
        return scaffold_line

    if "<sep>" in result.caption:
        new_body, new_a_part = result.caption.split("<sep>", 1)
        new_body = _preserve_stereo_body_if_refactor_stripped(body, new_body)
        new_extension = new_a_part + rest + pipe_ext
        expanded = f"{new_body}<sep>{new_extension}" if new_extension else new_body
    else:
        new_extension = rest + pipe_ext
        expanded = f"{result.caption}<sep>{new_extension}" if new_extension else result.caption

    if result.markush:
        log.append("模板缩写已部分展开，仍含 Markush 连接位。")
    else:
        log.append("模板缩写已全部展开为具体结构。")
    return expanded


def expand_reactant_template_lines(
    lines: list[str],
    messages: list[str] | None = None,
) -> list[str]:
    """Expand common group abbreviations in each reactant Markush template line."""
    return [expand_template_abbreviations(line, messages) for line in lines]
