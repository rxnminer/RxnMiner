"""Expand known abbreviations in template E-SMILES before Step0 load/draw."""
from __future__ import annotations

import re

from esmiles_loader import split_esmiles
from rdkit import Chem
from translator import Translator

_A_TAG = re.compile(r"<a>\d+:[^<]*</a>")
_PIPE_EXT = re.compile(r"(\|[^|]+\|)$")


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


def expand_template_abbreviations(
    scaffold_line: str,
    messages: list[str] | None = None,
) -> str:
    """Run vendored ``Translator.refactor`` on ``<a>`` tags; keep other extension tags."""
    log = messages if messages is not None else []
    line = scaffold_line.strip()
    if not line or "<sep>" not in line:
        return scaffold_line

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
