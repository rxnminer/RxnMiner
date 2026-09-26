"""Chiral center inventory and mapped stereo checks.

``check_mapped_stereo_peeled`` / ``check_mapped_stereo`` 仅诊断参考；全通过看 ``peel_topology_ok``（InChIKey）。
"""
from __future__ import annotations

from typing import Any

from rdkit import Chem

from models import StereoMismatch

def chiral_centers_map(mol: Chem.Mol) -> dict[int, str]:
    """Atom idx -> 'R' | 'S' | '?' from FindMolChiralCenters."""
    return {
        int(idx): str(label)
        for idx, label in Chem.FindMolChiralCenters(mol, includeUnassigned=True)
    }


def chiral_inventory_rows(mol: Chem.Mol) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, label in sorted(chiral_centers_map(mol).items()):
        sym = mol.GetAtomWithIdx(idx).GetSymbol()
        rows.append({"atom_idx": idx, "symbol": sym, "stereo": label})
    return rows


def format_chiral_summary(rows: list[dict[str, Any]], who: str) -> str:
    if not rows:
        return f"{who}：无手性中心（FindMolChiralCenters）。"
    parts = [f"{r['atom_idx']}({r['symbol']})={r['stereo']}" for r in rows]
    return f"{who}：{len(rows)} 个手性中心 — " + ", ".join(parts)


def scaffold_orig_to_new(scaffold_mol: Chem.Mol) -> dict[int, int]:
    """Original target atom idx -> atom idx in peeled scaffold mol."""
    out: dict[int, int] = {}
    for a in scaffold_mol.GetAtoms():
        if a.HasProp("_origIdx"):
            out[int(a.GetProp("_origIdx"))] = a.GetIdx()
    return out


def check_mapped_stereo_peeled(
    template_mol: Chem.Mol,
    scaffold_mol: Chem.Mol | None,
    template_to_full: dict[int, int],
) -> tuple[bool, list[StereoMismatch], list[dict[str, Any]]]:
    """Template vs peeled scaffold (product stereochemistry after removing substituents)."""
    if scaffold_mol is None:
        return (
            False,
            [
                StereoMismatch(
                    template_idx=0,
                    full_idx=-1,
                    template_stereo="—",
                    full_stereo="—",
                    note="无扣后骨架，无法比手性",
                )
            ],
            [],
        )
    orig2new = scaffold_orig_to_new(scaffold_mol)
    ct = chiral_centers_map(template_mol)
    cp = chiral_centers_map(scaffold_mol)
    peel_chiral = chiral_inventory_rows(scaffold_mol)
    mismatches: list[StereoMismatch] = []

    for t_idx, f_idx in template_to_full.items():
        p_idx = orig2new.get(f_idx)
        if p_idx is None:
            continue
        tl = ct.get(t_idx)
        pl = cp.get(p_idx)
        if pl is None:
            if tl is not None and tl != "?":
                mismatches.append(
                    StereoMismatch(
                        template_idx=t_idx,
                        full_idx=p_idx,
                        template_stereo=tl,
                        full_stereo="—",
                        note="模板该位为手性，扣后未识别",
                    )
                )
            continue
        if tl is None:
            mismatches.append(
                StereoMismatch(
                    template_idx=t_idx,
                    full_idx=p_idx,
                    template_stereo="—",
                    full_stereo=pl,
                    note="扣后骨架该位为手性，模板对应位不是",
                )
            )
            continue
        if tl == "?" or pl == "?":
            if tl != pl:
                mismatches.append(
                    StereoMismatch(
                        template_idx=t_idx,
                        full_idx=p_idx,
                        template_stereo=tl or "—",
                        full_stereo=pl or "—",
                        note="一侧立体未指定（?），另一侧已指定",
                    )
                )
            continue
        if tl != pl:
            mismatches.append(
                StereoMismatch(
                    template_idx=t_idx,
                    full_idx=p_idx,
                    template_stereo=tl,
                    full_stereo=pl,
                    note="R/S 不一致（扣后骨架）",
                )
            )

    return len(mismatches) == 0, mismatches, peel_chiral


def check_mapped_stereo(
    template_mol: Chem.Mol,
    target_mol: Chem.Mol,
    template_to_full: dict[int, int],
) -> tuple[bool, list[StereoMismatch]]:
    """Pass when every target chiral in the map matches template; extra template chirals OK."""
    ct = chiral_centers_map(template_mol)
    cf = chiral_centers_map(target_mol)
    mismatches: list[StereoMismatch] = []

    for t_idx, f_idx in template_to_full.items():
        tl = ct.get(t_idx)
        fl = cf.get(f_idx)
        if fl is None:
            continue
        if tl is None:
            mismatches.append(
                StereoMismatch(
                    template_idx=t_idx,
                    full_idx=f_idx,
                    template_stereo="—",
                    full_stereo=fl,
                    note="完整分子该位点为手性中心，模板对应原子不是",
                )
            )
            continue
        if tl == "?" or fl == "?":
            if tl != fl:
                mismatches.append(
                    StereoMismatch(
                        template_idx=t_idx,
                        full_idx=f_idx,
                        template_stereo=tl or "—",
                        full_stereo=fl or "—",
                        note="一侧立体未指定（?），另一侧已指定",
                    )
                )
            continue
        if tl != fl:
            mismatches.append(
                StereoMismatch(
                    template_idx=t_idx,
                    full_idx=f_idx,
                    template_stereo=tl,
                    full_stereo=fl,
                    note="R/S 不一致",
                )
            )

    return len(mismatches) == 0, mismatches
