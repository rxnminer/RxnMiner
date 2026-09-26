"""Step0 decompose facade: ``step0_scaffold_map`` and backward-compatible re-exports."""
from __future__ import annotations

from typing import Any

from rdkit import Chem

from assembly import (
    diagnose_target_smiles_failure,
    mol_to_png_base64,
    mol_to_png_with_atom_notes_base64,
    parse_fragment_line,
    peel_scaffold_depiction_mol,
)
from esmiles_ingress import load_target_mol, normalize_fragment_esmiles, normalize_target_input
from esmiles_loader import load_esmiles
from anchors import parse_anchors, template_dummy_slot_notes, template_dummy_to_rlabel, anchors_from_esmiles
from peel_topology import template_mol_from_smiles
from align import collect_all_alignments, template_heavy_query_mol
from candidates import (
    build_candidate,
    candidate_full_pass,
    candidate_roundtrip_pass,
    dedupe_by_index_map,
    dedupe_match_candidates_for_display,
)
from chem_util import heavy_atom_count
from models import (
    AlignmentMethodSummary,
    AnchorInfo,
    BondMismatch,
    ChiralFrameSnapshot,
    ENABLE_STEP0_ROUNDTRIP,
    MatchCandidate,
    PeelResult,
    PeeledSubstituent,
    Step0MapResult,
    StereoMismatch,
)
from peel import peel_substituents
from stereo import chiral_inventory_rows, format_chiral_summary

# --- peel_stereo_viz / tests: legacy private names ---
from align import (
    check_mapped_bond_orders,
    dedupe_template_to_full_maps,
    enumerate_substructure_alignments,
    enumerate_mcs_alignments,
    template_heavy_embeds,
)
from anchors import (
    dummy_attachment_bond_type,
    attachment_anchor_and_dummy,
    parse_anchors as _parse_anchors,
)
from peel import (
    non_core_components as _non_core_components,
    anchors_touching_component as _anchors_touching_component,
    boundary_bond_ids as _boundary_bond_ids,
    fragment_smiles_for_component as _fragment_smiles_for_component,
    greedy_expand_removal as _greedy_expand_removal,
    verify_peel_removal as _verify_peel_removal,
    compute_subst_roots as _compute_subst_roots,
    peel_embed_ok as _peel_embed_ok,
)
from scaffold_geom import (
    align_scaffold_2d_to_target as _align_scaffold_2d_to_target,
    anchor_bond_dirs_to_removed as _anchor_bond_dirs_to_removed,
    bond_dir_viewed_from_anchor as _bond_dir_viewed_from_anchor,
    build_scaffold_with_dummies as _build_scaffold_with_dummies,
    collect_chiral_frame_snapshots as _collect_chiral_frame_snapshots,
    copy_bond_dirs_from_product as _copy_bond_dirs_from_product,
    leaving_side_sign_2d as _leaving_side_sign_2d,
)
from stereo import scaffold_orig_to_new as _scaffold_orig_to_new
from r_groups import group_display_label
from roundtrip import LibraryContext

def step0_scaffold_map(
    target_smiles: str,
    scaffold_line: str,
    *,
    render_images: bool = True,
    library: LibraryContext | None = None,
) -> Step0MapResult:
    """Enumerate scaffold embeddings in target; validate bond orders per candidate."""
    messages: list[str] = []
    target_norm = normalize_target_input(target_smiles)
    target_smiles = target_norm.smiles
    if target_norm.note:
        messages.append(target_norm.note)
    if target_norm.mode == "fragment":
        messages.append("产物输入模式：fragment（部分 Markush / 可嵌套实例）。")
    else:
        messages.append("产物输入模式：concrete（完整具体分子）。")
    scaffold_line = normalize_fragment_esmiles(scaffold_line, messages)
    parsed = parse_fragment_line(scaffold_line)
    if not parsed:
        return Step0MapResult(
            ok=False,
            match_count=0,
            template_smiles="",
            template_heavy_smiles="",
            target_smiles=target_smiles,
            template_heavy_atoms=0,
            target_heavy_atoms=0,
            messages=["骨架行格式无效，需包含 <sep> 与标签。"],
        )

    template_smiles, tags = parsed
    esmiles_loaded = load_esmiles(scaffold_line, apply_dummy=True, prefer_indigo=True)
    if esmiles_loaded is None:
        return Step0MapResult(
            ok=False,
            match_count=0,
            template_smiles=template_smiles,
            template_heavy_smiles="",
            target_smiles=target_smiles,
            template_heavy_atoms=0,
            target_heavy_atoms=0,
            messages=["无法解析骨架 eSMILES SMILES 主体。"],
        )
    messages.extend(esmiles_loaded.messages)

    template_raw = esmiles_loaded.mol
    target_mol, target_load_notes = load_target_mol(target_norm)
    messages.extend(target_load_notes)
    if template_raw is None or target_mol is None:
        parse_msgs: list[str] = []
        if template_raw is None:
            parse_msgs.append("无法解析骨架 eSMILES SMILES 主体。")
        if target_mol is None:
            target_reason = diagnose_target_smiles_failure(target_smiles)
            parse_msgs.append(target_reason or "无法解析完整分子 SMILES。")
        return Step0MapResult(
            ok=False,
            match_count=0,
            template_smiles=template_smiles,
            template_heavy_smiles="",
            target_smiles=target_smiles,
            template_heavy_atoms=0,
            target_heavy_atoms=0,
            messages=parse_msgs or ["无法解析模板或完整分子 SMILES。"],
        )

    anchors = anchors_from_esmiles(template_raw, esmiles_loaded)
    if not anchors and tags:
        anchors = parse_anchors(template_raw, tags)
    template_dummy_map = template_dummy_to_rlabel(anchors)
    if template_dummy_map:
        template_mol = template_mol_from_smiles(template_smiles, template_dummy_map)
        if template_mol is None:
            return Step0MapResult(
                ok=False,
                match_count=0,
                template_smiles=template_smiles,
                template_heavy_smiles="",
                target_smiles=target_smiles,
                template_heavy_atoms=0,
                target_heavy_atoms=0,
                messages=[
                    "模板骨架 *→Ts（语义同位素）失败，请检查 <a> 标签与 SMILES。",
                ],
            )
        messages.append(
            f"模板已标注 Ts+语义同位素（{len(template_dummy_map)} 个连接位）。"
        )
    else:
        try:
            Chem.SanitizeMol(template_raw)
            template_mol = template_raw
        except Exception:
            template_mol = None
        if template_mol is None:
            return Step0MapResult(
                ok=False,
                match_count=0,
                template_smiles=template_smiles,
                template_heavy_smiles="",
                target_smiles=target_smiles,
                template_heavy_atoms=0,
                target_heavy_atoms=0,
                messages=["模板骨架 sanitize 失败。"],
            )

    query, _ = template_heavy_query_mol(template_mol)
    template_heavy_smi = Chem.MolToSmiles(query) if query else ""
    for a in anchors:
        kind_note = f" [{a.anchor_kind}]" if a.anchor_kind != "dummy" else ""
        messages.append(
            f"标签 {a.display_label} ({a.r_label}){kind_note} @ 模板锚点 {a.template_anchor_idx} "
            f"(dummy {a.template_dummy_idx}，配对键 {a.pair_key})"
        )
    template_chiral = chiral_inventory_rows(template_mol)
    target_chiral = chiral_inventory_rows(target_mol)

    messages.append(f"模板原子数（含 *）: {template_mol.GetNumAtoms()}")
    messages.append(f"去 * 骨架重原子数: {query.GetNumAtoms() if query else 0}")
    messages.append(f"完整分子重原子数: {heavy_atom_count(target_mol)}")
    target_kind = "fragment 产物" if target_norm.mode == "fragment" else "完整分子"
    messages.append(format_chiral_summary(target_chiral, target_kind))
    messages.append(format_chiral_summary(template_chiral, "模板"))

    template_heavy_n = query.GetNumAtoms() if query else 0
    alignment_rows = collect_all_alignments(template_mol, target_mol)

    messages.append(
        f"B·子结构（去*骨架 ⊂ 产物）：{sum(1 for s, _, _ in alignment_rows if s == 'B·子结构')} 种嵌入。"
    )
    messages.append(
        f"A·MCS键级（CompareOrder）：{sum(1 for s, _, _ in alignment_rows if s == 'A·MCS键级')} 种；"
        f"A·MCS拓扑（CompareAny）：{sum(1 for s, _, _ in alignment_rows if s == 'A·MCS拓扑')} 种。"
    )

    matches: list[MatchCandidate] = []
    for i, (source, t2f_int, _meta) in enumerate(alignment_rows):
        cand = build_candidate(
            index=i,
            source=source,
            t2f_int=t2f_int,
            template_mol=template_mol,
            target_mol=target_mol,
            anchors=anchors,
            render_images=render_images,
            target_smiles=target_smiles,
            library=library,
            attachment_contract=esmiles_loaded.attachment_contract,
            atom_by_position=esmiles_loaded.atom_by_position,
        )
        matches.append(cand)

    match_count_raw = len(matches)
    raw_matches = list(matches)
    index_deduped = dedupe_by_index_map(raw_matches)
    match_count_index = len(index_deduped)
    matches = dedupe_match_candidates_for_display(raw_matches)

    summary_by_method: dict[str, AlignmentMethodSummary] = {}
    for c in raw_matches:
        m = c.source
        if m not in summary_by_method:
            summary_by_method[m] = AlignmentMethodSummary(
                method=m,
                candidate_count=0,
                bond_ok_count=0,
                full_pass_count=0,
                max_mapped_atoms=0,
            )
        s = summary_by_method[m]
        s.candidate_count += 1
        if c.bond_order_ok:
            s.bond_ok_count += 1
        if candidate_full_pass(c):
            s.full_pass_count += 1
        s.max_mapped_atoms = max(s.max_mapped_atoms, c.mapped_atoms)

    alignment_summary = [
        summary_by_method[k]
        for k in ("B·子结构", "A·MCS键级", "A·MCS拓扑")
        if k in summary_by_method
    ]
    for s in alignment_summary:
        messages.append(
            f"  [{s.method}] {s.candidate_count} 候选，键级✓ {s.bond_ok_count}，"
            f"全通过 {s.full_pass_count}，最大映射 {s.max_mapped_atoms} 原子"
            f"（模板去* {template_heavy_n}）"
        )

    n_unique = len(matches)
    n_bond_ok = sum(1 for c in matches if c.bond_order_ok)
    n_stereo_ok = sum(1 for c in matches if c.stereo_ok)
    n_peel_topology_ok = sum(1 for c in matches if c.peel_topology_ok)
    n_virtual_arc_ok = sum(1 for c in matches if c.virtual_arc_ok)
    n_peel_stereo_ok = sum(1 for c in matches if c.peel_stereo_ok)
    n_full_pass = sum(1 for c in matches if candidate_full_pass(c))
    n_roundtrip_pass = (
        sum(1 for c in matches if candidate_roundtrip_pass(c))
        if ENABLE_STEP0_ROUNDTRIP
        else 0
    )
    for c in matches:
        alt = [s for s in c.also_sources if s != c.source]
        alt_txt = f"（亦见于 {', '.join(alt)}）" if alt else ""
        merge_txt = (
            f"（红点相同，已合并 {c.merged_map_count} 种 index 表）" if c.merged_map_count > 1 else ""
        )
        parts = [
            f"候选 #{c.index + 1} [{c.source}]：映射 {c.mapped_atoms} 原子{merge_txt}{alt_txt}"
        ]
        parts.append("键级✓" if c.bond_order_ok else f"键级×{c.bond_mismatch_count}")
        if c.virtual_arc_checks:
            parts.append("虚拟弧✓" if c.virtual_arc_ok else "虚拟弧×")
        parts.append("全分子手性✓" if c.stereo_ok else f"全分子手性×{c.stereo_mismatch_count}")
        parts.append("扣后结构✓" if c.peel_topology_ok else "扣后结构×")
        if ENABLE_STEP0_ROUNDTRIP:
            parts.append("组装✓" if c.roundtrip_ok else "组装×")
        parts.append(
            ("扣后手性✓" if c.peel_stereo_ok else f"扣后手性×{c.peel_stereo_mismatch_count}")
            + "（参考）"
        )
        messages.append(" · ".join(parts))

    ok = n_unique > 0
    if ok:
        best = matches[0]
        messages.insert(
            0,
            f"展示 {n_unique} 种不同红点区域（原始 {match_count_raw}，index 表 {match_count_index} 种，"
            f"按产物高亮去重）；键级✓ {n_bond_ok}；虚拟弧✓ {n_virtual_arc_ok}；全分子手性✓ {n_stereo_ok}（参考）；"
            f"扣后结构✓ {n_peel_topology_ok}；扣后手性✓ {n_peel_stereo_ok}（CIP 参考）；"
            f"全通过(键级+虚拟弧+扣后结构) {n_full_pass}"
            + (
                f"；组装回目标 {n_roundtrip_pass}。默认：{best.source}。"
                if ENABLE_STEP0_ROUNDTRIP
                else f"。默认：{best.source}。（组装验证已关闭）"
            ),
        )
    else:
        messages.insert(0, "B 与 A 均未找到嵌入候选。")

    return Step0MapResult(
        ok=ok,
        match_count=n_unique,
        match_count_raw=match_count_raw,
        match_count_index=match_count_index,
        template_smiles=template_smiles,
        template_heavy_smiles=template_heavy_smi,
        target_smiles=target_smiles,
        template_heavy_atoms=template_heavy_n,
        target_heavy_atoms=heavy_atom_count(target_mol),
        matches=matches,
        messages=messages,
        template_image_base64=(
            mol_to_png_with_atom_notes_base64(
                peel_scaffold_depiction_mol(template_mol),
                size=(400, 280),
                atom_notes=template_dummy_slot_notes(anchors),
                stereo_annotation=True,
            )
            if render_images
            else ""
        ),
        target_image_base64=(
            mol_to_png_base64(target_mol, size=(520, 360)) if render_images else ""
        ),
        highlight_image_base64=(
            matches[0].highlight_image_base64 if matches and render_images else ""
        ),
        template_chiral_centers=template_chiral,
        target_chiral_centers=target_chiral,
        alignment_summary=alignment_summary,
    )
