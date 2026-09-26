"""Helpers for adding rows to ``translator/abbrevs.csv``."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rdkit import Chem
from rdkit.Chem import AllChem
from rdkit.Chem.Draw import rdMolDraw2D

ABBREV_CSV = Path(__file__).resolve().parent / "translator" / "abbrevs.csv"
CSV_FIELDS = ("name", "smiles", "atoms", "symbol", "label", "labelW")


@dataclass(frozen=True)
class AtomInfo:
    idx: int
    symbol: str
    degree: int
    is_aromatic: bool


@dataclass(frozen=True)
class StructurePreview:
    smiles: str
    atoms: list[AtomInfo]
    attach_hint: int | None
    image_base64: str


@dataclass(frozen=True)
class AbbrevDraft:
    name: str
    smiles: str
    atoms: int
    symbol: str
    label: str
    labelW: str
    attach_idx: int
    duplicate_symbols: list[str]


def _strip_smiles(smiles: str) -> str:
    return (smiles or "").strip()


def _mol_image_base64(mol: Chem.Mol, *, highlight: int | None = None, size: tuple[int, int] = (520, 360)) -> str:
    import base64

    draw_mol = Chem.Mol(mol)
    if draw_mol.GetNumConformers() == 0:
        AllChem.Compute2DCoords(draw_mol)

    w, h = size
    drawer = rdMolDraw2D.MolDraw2DCairo(w, h)
    opts = drawer.drawOptions()
    opts.addAtomIndices = True
    opts.addStereoAnnotation = True
    highlight_atoms = [highlight] if highlight is not None else []
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, draw_mol, highlightAtoms=highlight_atoms)
    drawer.FinishDrawing()
    return base64.b64encode(drawer.GetDrawingText()).decode("ascii")


def normalize_input_mol(smiles: str) -> tuple[Chem.Mol, int | None]:
    """Parse SMILES and remove any ``*`` dummy; return mol and hinted attachment idx."""
    smi = _strip_smiles(smiles)
    if not smi:
        raise ValueError("SMILES 为空")

    mol = Chem.MolFromSmiles(smi)
    if mol is None:
        raise ValueError(f"无法解析 SMILES: {smi}")

    attach_hint: int | None = None
    rw = Chem.RWMol(mol)
    dummy_idxs = [a.GetIdx() for a in rw.GetAtoms() if a.GetSymbol() == "*"]
    for dummy_idx in sorted(dummy_idxs, reverse=True):
        dummy = rw.GetAtomWithIdx(dummy_idx)
        neighbors = list(dummy.GetNeighbors())
        if neighbors and attach_hint is None:
            attach_hint = neighbors[0].GetIdx()
            if dummy_idx < attach_hint:
                attach_hint -= 1
        rw.RemoveAtom(dummy_idx)

    out = Chem.Mol(rw)
    Chem.SanitizeMol(out)
    return out, attach_hint


def count_abbrev_atoms(mol: Chem.Mol) -> int:
    """Heavy-atom count used in abbrevs.csv (includes ``*`` if present)."""
    n = mol.GetNumHeavyAtoms()
    n += sum(1 for atom in mol.GetAtoms() if atom.GetSymbol() == "*")
    return n


def make_abbrev_smiles(mol: Chem.Mol, attach_idx: int) -> str:
    if not 0 <= attach_idx < mol.GetNumAtoms():
        raise ValueError(f"取代位点索引越界: {attach_idx}")
    atom = mol.GetAtomWithIdx(attach_idx)
    if atom.GetSymbol() == "*":
        raise ValueError("请选择真实原子，而非 * 占位符")
    return Chem.MolToSmiles(mol, rootedAtAtom=attach_idx, canonical=False)


def preview_structure(smiles: str, *, highlight_idx: int | None = None) -> StructurePreview:
    mol, attach_hint = normalize_input_mol(smiles)
    atoms = [
        AtomInfo(
            idx=i,
            symbol=atom.GetSymbol(),
            degree=atom.GetDegree(),
            is_aromatic=atom.GetIsAromatic(),
        )
        for i, atom in enumerate(mol.GetAtoms())
    ]
    return StructurePreview(
        smiles=Chem.MolToSmiles(mol, canonical=False),
        atoms=atoms,
        attach_hint=attach_hint,
        image_base64=_mol_image_base64(mol, highlight=highlight_idx),
    )


def _default_label_fields(symbol: str) -> tuple[str, str]:
    return symbol, symbol


def build_draft(
    smiles: str,
    attach_idx: int,
    name: str,
    *,
    symbol: str | None = None,
    label: str | None = None,
    labelW: str | None = None,
) -> AbbrevDraft:
    name = (name or "").strip()
    if not name:
        raise ValueError("name 不能为空")

    mol, _ = normalize_input_mol(smiles)
    abbrev_smi = make_abbrev_smiles(mol, attach_idx)
    abbrev_mol = Chem.MolFromSmiles(abbrev_smi)
    if abbrev_mol is None:
        raise ValueError(f"生成的缩写 SMILES 无效: {abbrev_smi}")

    sym = (symbol or name).strip()
    if not sym:
        raise ValueError("symbol 不能为空")

    lbl, lblw = _default_label_fields(sym)
    if label is not None and label.strip():
        lbl = label.strip()
    if labelW is not None and labelW.strip():
        lblw = labelW.strip()

    duplicates = find_rows_by_symbol(sym)
    dup_symbols = [row["symbol"] for row in duplicates if row.get("symbol")]

    return AbbrevDraft(
        name=name,
        smiles=abbrev_smi,
        atoms=count_abbrev_atoms(abbrev_mol),
        symbol=sym,
        label=lbl,
        labelW=lblw,
        attach_idx=attach_idx,
        duplicate_symbols=dup_symbols,
    )


def draft_to_row(draft: AbbrevDraft) -> dict[str, str]:
    return {
        "name": draft.name,
        "smiles": draft.smiles,
        "atoms": str(draft.atoms),
        "symbol": draft.symbol,
        "label": draft.label,
        "labelW": draft.labelW,
    }


def load_all_rows() -> list[dict[str, str]]:
    if not ABBREV_CSV.is_file():
        return []
    with ABBREV_CSV.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def find_rows_by_symbol(symbol: str) -> list[dict[str, str]]:
    sym = (symbol or "").strip()
    if not sym:
        return []
    return [row for row in load_all_rows() if (row.get("symbol") or "").strip() == sym]


def search_symbols(query: str, *, limit: int = 30) -> list[dict[str, str]]:
    q = (query or "").strip().lower()
    if not q:
        return []
    out: list[dict[str, str]] = []
    for row in load_all_rows():
        sym = (row.get("symbol") or "").strip()
        name = (row.get("name") or "").strip()
        if q in sym.lower() or q in name.lower():
            out.append(row)
            if len(out) >= limit:
                break
    return out


def append_rows(rows: list[dict[str, str]]) -> int:
    if not rows:
        return 0
    for row in rows:
        for field in CSV_FIELDS:
            if field not in row or not str(row[field]).strip():
                raise ValueError(f"行缺少字段 {field}: {row}")

    ABBREV_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not ABBREV_CSV.exists() or ABBREV_CSV.stat().st_size == 0
    with ABBREV_CSV.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({field: str(row[field]).strip() for field in CSV_FIELDS})
    return len(rows)


def merge_preview_smiles(abbrev_smi: str) -> str:
    """Attach abbrev onto ``CC*`` to sanity-check graft direction."""
    from translator import _utils as tu

    scaffold = Chem.RWMol(Chem.MolFromSmiles("CC*"))
    if scaffold is None:
        raise ValueError("内部测试骨架无效")
    dummy_idx = next(i for i, a in enumerate(scaffold.GetAtoms()) if a.GetSymbol() == "*")
    tu.merge_group(scaffold, abbrev_smi, dummy_idx)
    return Chem.MolToSmiles(scaffold, canonical=False)


def atom_list_dict(preview: StructurePreview) -> list[dict[str, Any]]:
    return [
        {
            "idx": atom.idx,
            "symbol": atom.symbol,
            "degree": atom.degree,
            "is_aromatic": atom.is_aromatic,
        }
        for atom in preview.atoms
    ]
