"""Reverse assembly: peel R-groups from product, fill reactant Markush templates."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from rdkit import Chem

from chemistry.assembly import (
    assemble_fragments,
    normalize_target_smiles,
)
from esmiles import expand_reactant_template_lines
from chemistry.candidates import (
    candidate_full_pass,
    candidate_roundtrip_pass,
    candidate_sort_key,
)
from chemistry.decompose import (
    step0_scaffold_map,
    step0_scaffold_map_resolving_repeats,
)
from esmiles import normalize_fragment_esmiles
from esmiles import ESMILES_SEPARATOR, load_esmiles, parse_fragment_line
from chemistry.models import MatchCandidate
from chemistry.peel_topology import (
    AttachmentDuplicateLabelResult,
    AttachmentLabelSymmetryResult,
    check_attachment_label_symmetry,
    check_peeled_duplicate_label_substituents,
    check_template_duplicate_attachment_labels,
    template_mol_from_smiles,
)
from chemistry.r_groups import parse_tag_string, short_display_from_source

_TAG_REPLACE = re.compile(r"(<a>\d+:)([^<]+)(</a>)")


@dataclass
class RGroupAssignment:
    label: str
    display_label: str
    smiles_line: str
    heavy_atoms: int
    slot_ids: list[str] = field(default_factory=list)
    image_base64: str = ""


@dataclass
class ReactantAssemblyRow:
    index: int
    template_line: str
    labels_needed: list[str]
    fragment_lines: list[str]
    assembled_smiles: str
    success: bool
    partial: bool
    messages: list[str] = field(default_factory=list)
    image_base64: str = ""
    missing_labels: list[str] = field(default_factory=list)
    expected_smiles: str = ""
    expected_image_base64: str = ""
    match_expected: bool | None = None
    match_note: str = ""


@dataclass
class ReverseAssemblyResult:
    ok: bool
    notes: list[str] = field(default_factory=list)
    target_smiles: str = ""
    product_scaffold_line: str = ""
    resolved_product_scaffold_line: str = ""
    repeat_assignments: list[dict[str, Any]] = field(default_factory=list)
    match_index: int = -1
    match_source: str = ""
    peel_ok: bool = False
    peel_topology_note: str = ""
    peel_topology_canonical_template: str = ""
    peel_topology_canonical_peeled: str = ""
    peel_topology_inchi_template: str = ""
    peel_topology_inchi_peeled: str = ""
    peel_topology_nonisomeric_match: bool = False
    peel_topology_stereo_missing_hint: bool = False
    roundtrip_ok: bool = False
    roundtrip_note: str = ""
    label_side_relations_ok: bool = False
    label_side_relation_notes: list[str] = field(default_factory=list)
    r_groups: list[RGroupAssignment] = field(default_factory=list)
    reactants: list[ReactantAssemblyRow] = field(default_factory=list)
    peeled_substituents: list[dict[str, Any]] = field(default_factory=list)
    assembly_fragment_lines: list[str] = field(default_factory=list)
    yield_text: str = ""
    template_id: str = ""
    markush_enumeration: dict[str, str] = field(default_factory=dict)
    compare_ok: bool | None = None
    unmatched_json_reactants: list[dict[str, Any]] = field(default_factory=list)
    attachment_label_symmetry_warn: bool = False
    attachment_label_symmetry_groups: list[list[str]] = field(default_factory=list)
    attachment_label_symmetry_note: str = ""
    attachment_duplicate_label_warn: bool = False
    attachment_duplicate_label_groups: list[dict[str, Any]] = field(default_factory=list)
    attachment_duplicate_label_note: str = ""
    duplicate_label_peel_warn: bool = False
    duplicate_label_peel_groups: list[dict[str, Any]] = field(default_factory=list)
    duplicate_label_peel_note: str = ""
    template_image_base64: str = ""
    target_image_base64: str = ""
    peel_highlight_image_base64: str = ""
    peel_scaffold_image_base64: str = ""
    peel_scaffold_smiles: str = ""
    peel_bond_order_ok: bool = True
    peel_match_count: int = 0
    roundtrip_image_base64: str = ""


@lru_cache(maxsize=512)
def template_attachment_label_symmetry(scaffold_line: str) -> AttachmentLabelSymmetryResult:
    """Check product Markush template once; result is shared by all examples on that template."""
    line = (scaffold_line or "").strip()
    if not line or ESMILES_SEPARATOR not in line:
        return AttachmentLabelSymmetryResult(False, [], "")

    parsed = parse_fragment_line(line)
    if not parsed:
        return AttachmentLabelSymmetryResult(False, [], "")

    template_smiles, tags = parsed
    loaded = load_esmiles(line, apply_dummy=True, prefer_indigo=True)
    if loaded is None or loaded.mol is None:
        return AttachmentLabelSymmetryResult(False, [], "")

    from chemistry.anchors import anchors_from_esmiles, parse_anchors, template_dummy_to_rlabel

    anchors = anchors_from_esmiles(loaded.mol, loaded)
    if not anchors and tags:
        anchors = parse_anchors(loaded.mol, tags)
    dummy_map = template_dummy_to_rlabel(anchors)
    if len(dummy_map) < 2:
        return AttachmentLabelSymmetryResult(False, [], "")

    mol = template_mol_from_smiles(template_smiles, dummy_map)
    if mol is None:
        return AttachmentLabelSymmetryResult(False, [], "")

    return check_attachment_label_symmetry(mol, dummy_map)


def attachment_label_symmetry_payload(scaffold_line: str) -> dict[str, Any]:
    sym = template_attachment_label_symmetry((scaffold_line or "").strip())
    return {
        "attachment_label_symmetry_warn": sym.warn,
        "attachment_label_symmetry_groups": [list(g) for g in sym.symmetric_groups],
        "attachment_label_symmetry_note": sym.note,
    }


@lru_cache(maxsize=512)
def template_attachment_duplicate_labels(scaffold_line: str) -> AttachmentDuplicateLabelResult:
    """Check product Markush template once for repeated connection labels."""
    line = (scaffold_line or "").strip()
    if not line or ESMILES_SEPARATOR not in line:
        return AttachmentDuplicateLabelResult(False, [], "")
    parsed = parse_fragment_line(line)
    if not parsed:
        return AttachmentDuplicateLabelResult(False, [], "")
    return check_template_duplicate_attachment_labels(parsed[1])


def attachment_duplicate_label_payload(scaffold_line: str) -> dict[str, Any]:
    dup = template_attachment_duplicate_labels((scaffold_line or "").strip())
    return {
        "attachment_duplicate_label_warn": dup.warn,
        "attachment_duplicate_label_groups": list(dup.duplicate_groups),
        "attachment_duplicate_label_note": dup.note,
    }


def template_warning_payload(scaffold_line: str) -> dict[str, Any]:
    return {
        **attachment_label_symmetry_payload(scaffold_line),
        **attachment_duplicate_label_payload(scaffold_line),
    }


def _label_aliases(label: str) -> set[str]:
    raw = (label or "").strip()
    if not raw:
        return set()
    out = {raw, short_display_from_source(raw)}
    m = re.match(r"^R\[([^\]]+)\]$", raw, re.IGNORECASE)
    if m:
        inner = m.group(1).strip()
        out.add(f"R{inner}")
        out.add(f"R[{inner}]")
    m2 = re.match(r"^R(\d+)$", raw, re.IGNORECASE)
    if m2:
        out.add(f"R[{m2.group(1)}]")
        out.add(f"R{m2.group(1)}")
    compact = re.sub(r"[\[\](){}\s_]", "", raw).lower()
    # Protocol and historical corpus spellings for the same abstract repeat.
    if compact in {"ch2?n", "ch2n"}:
        out.update({"CH2?n", "CH[2]?n", "(CH2)n"})
    return {x for x in out if x}


def labels_equivalent(a: str, b: str) -> bool:
    return bool(_label_aliases(a) & _label_aliases(b))


def labels_from_template_line(line: str) -> list[str]:
    parsed = parse_fragment_line(line)
    if not parsed:
        return []
    return [lbl for _, lbl in parse_tag_string(parsed[1])]


def relabel_fragment_line(line: str, target_label: str) -> str:
    line = (line or "").strip()
    parsed = parse_fragment_line(line)
    if parsed is None:
        smiles = line.split(ESMILES_SEPARATOR, 1)[0].strip()
        tags = ""
    else:
        smiles, tags = parsed
    if not tags.strip():
        mol = Chem.MolFromSmiles(smiles)
        if mol is not None:
            dummies = [a.GetIdx() for a in mol.GetAtoms() if a.GetAtomicNum() == 0]
            if dummies:
                from esmiles import encode_substituent_esmiles

                return encode_substituent_esmiles(mol, {dummies[0]: target_label})
        return smiles
    new_tags = _TAG_REPLACE.sub(
        lambda m: f"{m.group(1)}{target_label}{m.group(3)}",
        tags,
    )
    return f"{smiles}{ESMILES_SEPARATOR}{new_tags}"


def relabel_template_slot_line(line: str, source_label: str, target_label: str) -> str:
    """Replace only the matching template slot label, preserving other slots."""
    line = (line or "").strip()
    parsed = parse_fragment_line(line)
    if parsed is None:
        return line
    smiles, tags = parsed
    tag_list = parse_tag_string(tags)
    if not tag_list:
        return line
    new_tags = [
        (pos, target_label if labels_equivalent(lbl, source_label) else lbl)
        for pos, lbl in tag_list
    ]
    tag_str = "".join(f"<a>{pos}:{lbl}</a>" for pos, lbl in new_tags)
    return f"{smiles}{ESMILES_SEPARATOR}{tag_str}"


def _substituent_slot_labels(item: dict[str, Any]) -> list[str]:
    """Ordered connection labels for a peeled substituent (one per anchor)."""
    slot_ids = [str(s).strip() for s in (item.get("slot_ids") or []) if str(s).strip()]
    if len(slot_ids) > 1:
        return slot_ids
    r_label = str(item.get("r_label") or "").strip()
    if "," in r_label:
        parts = [p.strip() for p in r_label.split(",") if p.strip()]
        if len(parts) > 1:
            return parts
    parsed = parse_fragment_line((item.get("smiles") or "").strip())
    if parsed:
        tags = [lbl for _, lbl in parse_tag_string(parsed[1])]
        if len(tags) > 1:
            return tags
    if slot_ids:
        return slot_ids
    return [r_label] if r_label else []


def _is_bridge_substituent(item: dict[str, Any]) -> bool:
    return len(_substituent_slot_labels(item)) >= 2


def _canonical_template_label(template_labels: list[str], slot: str) -> str:
    for tpl in template_labels:
        if labels_equivalent(tpl, slot):
            return tpl
    return slot


def remap_bridge_substituent_line(
    line: str,
    peel_slots: list[str],
    template_labels: list[str],
) -> str:
    """Map each bridge end to the reactant template label (keep per-end tags)."""
    line = (line or "").strip()
    parsed = parse_fragment_line(line)
    if parsed is None:
        return line
    smiles, tags = parsed
    if not tags.strip():
        return line
    tag_list = parse_tag_string(tags)
    if not tag_list:
        return line

    slot_to_tpl = {
        slot: _canonical_template_label(template_labels, slot)
        for slot in peel_slots
    }
    distinct = {lbl for _, lbl in tag_list}
    if len(tag_list) >= 2 and len(distinct) == 1:
        ordered_tpls = [slot_to_tpl[slot] for slot in peel_slots]
        if len(ordered_tpls) == 1:
            new_tags = [(pos, ordered_tpls[0]) for pos, _ in tag_list]
        elif len(ordered_tpls) == len(tag_list):
            new_tags = [
                (pos, ordered_tpls[i])
                for i, (pos, _) in enumerate(sorted(tag_list, key=lambda x: x[0]))
            ]
        else:
            # Malformed/ambiguous multi-port evidence must not crash corpus
            # evaluation. Preserve the original semantic label so assembly can
            # return an explicit partial/failure result.
            new_tags = list(tag_list)
    else:
        new_tags = []
        for pos, tag_lbl in tag_list:
            tpl = _canonical_template_label(template_labels, tag_lbl)
            for slot in peel_slots:
                if labels_equivalent(tag_lbl, slot):
                    tpl = slot_to_tpl[slot]
                    break
            new_tags.append((pos, tpl))
    new_tags.sort(key=lambda x: x[0])
    tag_str = "".join(f"<a>{pos}:{lbl}</a>" for pos, lbl in new_tags)
    return f"{smiles}{ESMILES_SEPARATOR}{tag_str}"


def _slots_matching_needed(
    slots: list[str],
    needed: list[str],
    covered: set[int],
) -> list[tuple[str, str]]:
    """Pair peel slots with uncovered reactant-template labels."""
    out: list[tuple[str, str]] = []
    used_needed: set[int] = set()
    for slot in slots:
        for j, tpl_label in enumerate(needed):
            if j in covered or j in used_needed:
                continue
            if labels_equivalent(slot, tpl_label):
                out.append((slot, tpl_label))
                used_needed.add(j)
                break
    return out


def _item_matches_label(item: dict[str, Any], label: str) -> bool:
    r_label = str(item.get("r_label") or "")
    slot_ids = [str(s) for s in (item.get("slot_ids") or [])]
    if labels_equivalent(r_label, label):
        return True
    return any(labels_equivalent(s, label) for s in slot_ids)


def _find_single_substituent_for_label(
    pool: list[dict[str, Any]],
    label: str,
    used: set[int],
) -> dict[str, Any] | None:
    """Prefer single-anchor substituents; skip bridge fragments."""
    for i, item in enumerate(pool):
        if i in used or _is_bridge_substituent(item):
            continue
        if _item_matches_label(item, label):
            return item
    return None


def _reactants_assembly_pass_count(
    match: MatchCandidate,
    reactant_templates: list[str],
    product_template: str = "",
) -> int:
    count = 0
    for i, tpl in enumerate(reactant_templates):
        if product_template:
            from chemistry.ring_transfer import materialize_transferred_ring_slots

            transferred = materialize_transferred_ring_slots(
                product_template, tpl, match.anchors
            )
            if transferred.unresolved_labels:
                continue
            tpl = transferred.template_line
        row = assemble_reactant_from_substituents(
            tpl,
            match.peeled_substituents,
            render_images=False,
            index=i,
        )
        if row.success:
            count += 1
    return count


def _pick_best_match(
    matches: list[MatchCandidate],
    reactant_templates: list[str] | None = None,
) -> MatchCandidate | None:
    if not matches:
        return None
    templates = [t.strip() for t in (reactant_templates or []) if (t or "").strip()]

    def sort_key(c: MatchCandidate) -> tuple:
        r_all = False
        r_count = 0
        if templates:
            r_count = _reactants_assembly_pass_count(c, templates)
            r_all = r_count == len(templates)
        return (
            not r_all,
            -r_count,
            not candidate_roundtrip_pass(c),
            not candidate_full_pass(c),
            candidate_sort_key(c),
        )

    ranked = sorted(matches, key=sort_key)
    return ranked[0]


def _find_substituent_for_label(
    pool: list[dict[str, Any]],
    label: str,
    used_ids: set[int],
) -> dict[str, Any] | None:
    item = _find_single_substituent_for_label(pool, label, used_ids)
    if item is not None:
        return item
    for i, item in enumerate(pool):
        if i in used_ids:
            continue
        if _item_matches_label(item, label):
            return item
    return None


def _assignments_from_peeled(
    peeled: list[dict[str, Any]],
    *,
    render_images: bool,
) -> list[RGroupAssignment]:
    rows: list[RGroupAssignment] = []
    for item in _substituent_entries(peeled):
        smi_line = (item.get("smiles") or "").strip()
        r_label = str(item.get("r_label") or "")
        display = str(item.get("display_label") or short_display_from_source(r_label))
        slot_ids = [str(s) for s in (item.get("slot_ids") or [])]
        img = ""
        if render_images and smi_line:
            img = structure_png_base64(smi_line, render=True)
        rows.append(
            RGroupAssignment(
                label=r_label,
                display_label=display,
                smiles_line=smi_line,
                heavy_atoms=int(item.get("heavy_atoms") or 0),
                slot_ids=slot_ids,
                image_base64=img,
            )
        )
    return rows


def _substituent_entries(peeled: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in peeled:
        smi = (item.get("smiles") or "").strip()
        if not smi:
            continue
        out.append(item)
    return out


def assemble_reactant_from_substituents(
    reactant_template: str,
    peeled: list[dict[str, Any]],
    *,
    render_images: bool = False,
    index: int = 0,
    symmetric_swap: bool = False,
    symmetric_pair: tuple[str, str] | None = None,
) -> ReactantAssemblyRow:
    template_line = reactant_template.strip()
    if ESMILES_SEPARATOR not in template_line:
        mol = Chem.MolFromSmiles(template_line)
        if render_images and mol is not None:
            from chemistry.rendering import render_display_mol

            img = render_display_mol(mol, size=(520, 360))
        else:
            img = ""
        return ReactantAssemblyRow(
            index=index,
            template_line=template_line,
            labels_needed=[],
            fragment_lines=[template_line],
            assembled_smiles=template_line,
            success=mol is not None,
            partial=False,
            messages=[] if mol is not None else ["无法解析具体反应物 SMILES"],
            image_base64=img,
            missing_labels=[],
        )

    needed = labels_from_template_line(reactant_template)
    pool = _substituent_entries(peeled)
    used: set[int] = set()
    covered_needed: set[int] = set()
    fragment_lines = [reactant_template.strip()]
    missing: list[str] = []
    pick_notes: list[str] = []

    def _peel_lookup_for_template(template_label: str) -> str:
        if not symmetric_swap or not symmetric_pair or len(symmetric_pair) != 2:
            return template_label
        a, b = symmetric_pair
        if labels_equivalent(template_label, a):
            return b
        if labels_equivalent(template_label, b):
            return a
        return template_label

    for i, item in enumerate(pool):
        if i in used:
            continue
        slots = _substituent_slot_labels(item)
        if len(slots) < 2:
            continue
        pairs = _slots_matching_needed(slots, needed, covered_needed)
        # A degree-2 placeholder is represented by one template dummy/tag but
        # the peeled replacement has two attachment ports carrying that same
        # semantic label.  It consumes one template slot, not two labels.
        if len(pairs) == 1 and all(labels_equivalent(s, slots[0]) for s in slots):
            peel_label, tpl_label = pairs[0]
            sub_line = relabel_fragment_line(
                (item.get("smiles") or "").strip(), tpl_label
            )
            fragment_lines.append(sub_line)
            used.add(i)
            for j, needed_label in enumerate(needed):
                if j not in covered_needed and labels_equivalent(needed_label, tpl_label):
                    covered_needed.add(j)
                    break
            pick_notes.append(
                f"桥连 {peel_label} ← {item.get('display_label') or item.get('r_label')} "
                f"→ {tpl_label} ({sub_line.split(ESMILES_SEPARATOR, 1)[0][:48]})"
            )
            continue
        if len(pairs) < 2:
            continue
        peel_slots = [slot for slot, _ in pairs]
        tpl_labels = [tpl for _, tpl in pairs]
        sub_line = remap_bridge_substituent_line(
            (item.get("smiles") or "").strip(),
            peel_slots,
            tpl_labels,
        )
        fragment_lines.append(sub_line)
        used.add(i)
        for j, tpl_label in enumerate(needed):
            if any(labels_equivalent(tpl_label, t) for _, t in pairs):
                covered_needed.add(j)
        pick_notes.append(
            f"桥连 {'+'.join(peel_slots)} ← "
            f"{item.get('display_label') or item.get('r_label')} "
            f"→ {'+'.join(tpl_labels)} "
            f"({sub_line.split(ESMILES_SEPARATOR, 1)[0][:48]})"
        )

    for j, label in enumerate(needed):
        if j in covered_needed:
            continue
        peel_lookup = _peel_lookup_for_template(label)
        item = _find_single_substituent_for_label(pool, peel_lookup, used)
        if item is None:
            missing.append(label)
            continue
        idx = pool.index(item)
        used.add(idx)
        open_placeholder = (item.get("open_placeholder_label") or "").strip()
        if open_placeholder:
            fragment_lines[0] = relabel_template_slot_line(
                fragment_lines[0],
                label,
                open_placeholder,
            )
            covered_needed.add(j)
            missing.append(open_placeholder)
            pick_notes.append(
                f"{label} ← {open_placeholder}（半通式开放位点，保留标签不按 H 处理）"
            )
            continue
        sub_line = relabel_fragment_line((item.get("smiles") or "").strip(), label)
        fragment_lines.append(sub_line)
        covered_needed.add(j)
        src = item.get("display_label") or item.get("r_label")
        if symmetric_swap and not labels_equivalent(peel_lookup, label):
            pick_notes.append(
                f"{label} ← {src}（对调枚举：取代基来自 {peel_lookup}）"
                f" ({sub_line.split(ESMILES_SEPARATOR, 1)[0][:48]})"
            )
        else:
            pick_notes.append(
                f"{label} ← {src} "
                f"({sub_line.split(ESMILES_SEPARATOR, 1)[0][:48]})"
            )

    asm = assemble_fragments(fragment_lines)
    img = structure_png_base64(asm.smiles, render=render_images) if asm.smiles else ""

    messages = list(pick_notes) + list(asm.messages)
    if missing:
        messages.insert(0, f"缺少取代基标签: {', '.join(missing)}")

    return ReactantAssemblyRow(
        index=index,
        template_line=reactant_template.strip(),
        labels_needed=needed,
        fragment_lines=fragment_lines,
        assembled_smiles=asm.smiles,
        success=asm.success,
        partial=asm.partial,
        messages=messages,
        image_base64=img,
        missing_labels=missing,
    )


def reverse_assemble(
    target_smiles: str,
    product_scaffold_line: str,
    reactant_templates: list[str],
    *,
    render_images: bool = True,
    match_index: int | None = None,
    expected_reactants: list[str] | None = None,
) -> ReverseAssemblyResult:
    """Peel product R-groups, then fill each reactant Markush template."""
    notes: list[str] = []
    target_norm_note = normalize_target_smiles(target_smiles)
    target_smiles, strip_note = target_norm_note
    if strip_note:
        notes.append(strip_note)

    product_scaffold_line = normalize_fragment_esmiles((product_scaffold_line or "").strip())
    reactant_templates = expand_reactant_template_lines(
        [r.strip() for r in reactant_templates if (r or "").strip()]
    )

    if not target_smiles:
        return ReverseAssemblyResult(ok=False, notes=["请提供实施例产物 SMILES。"])
    if not product_scaffold_line or ESMILES_SEPARATOR not in product_scaffold_line:
        return ReverseAssemblyResult(ok=False, notes=["请提供含 <sep> 的产物 Markush 模板。"])
    if not reactant_templates:
        return ReverseAssemblyResult(ok=False, notes=["请提供至少一条反应物 Markush 模板。"])

    resolved_step0 = step0_scaffold_map_resolving_repeats(
        target_smiles,
        product_scaffold_line,
        render_images=render_images,
    )
    notes.extend(resolved_step0.messages[:6])
    viable = [
        candidate
        for candidate in resolved_step0.candidates
        if candidate.result.ok and candidate.result.matches
    ]
    if not viable:
        # Resolution is intentionally strict (degree-2 carbon paths only).  A
        # legacy template may still use the repeat tag as a peelable bridge;
        # preserve the previously supported ordinary Step0 behavior in that
        # case instead of turning an unresolved abstraction into a hard error.
        fallback = step0_scaffold_map(
            target_smiles,
            product_scaffold_line,
            render_images=render_images,
        )
        if fallback.ok and fallback.matches:
            from chemistry.models import ResolvedRepeatStep0Candidate

            viable = [
                ResolvedRepeatStep0Candidate(
                    template_esmiles=product_scaffold_line,
                    repeat_assignments=[],
                    result=fallback,
                )
            ]
            notes.append("严格重复解析无候选；按历史桥占位语义继续拆分。")
    if not viable:
        step0_messages = [
            message
            for candidate in resolved_step0.candidates
            for message in candidate.result.messages[:3]
        ]
        peel_reason = next(
            (m for m in step0_messages + resolved_step0.messages if m),
            "未找到有效骨架嵌入。",
        )
        return ReverseAssemblyResult(
            ok=False,
            notes=notes + [f"产物拆分失败：{peel_reason}"],
            target_smiles=target_smiles,
            product_scaffold_line=product_scaffold_line,
        )

    # Rank repeat resolutions and ordinary embeddings together.  Reactant
    # assembly success is deliberately part of the rank because the purpose of
    # this pipeline is reverse construction, not scaffold overlap alone.
    ranked: list[tuple[tuple, Any, MatchCandidate]] = []
    for repeat_candidate in viable:
        for candidate_match in repeat_candidate.result.matches:
            templates = reactant_templates
            pass_count = _reactants_assembly_pass_count(
                candidate_match, templates, repeat_candidate.template_esmiles
            )
            ranked.append(
                (
                    (
                        -(pass_count == len(templates)),
                        -pass_count,
                        not candidate_roundtrip_pass(candidate_match),
                        not candidate_full_pass(candidate_match),
                        candidate_sort_key(candidate_match),
                    ),
                    repeat_candidate,
                    candidate_match,
                )
            )
    ranked.sort(key=lambda item: item[0])
    selected_repeat, default_match = ranked[0][1], ranked[0][2]
    step0 = selected_repeat.result
    notes.extend(step0.messages[:6])
    if selected_repeat.repeat_assignments:
        detail = ", ".join(
            f"{item['internal_id']}={item['count']}"
            for item in selected_repeat.repeat_assignments
        )
        notes.append(f"目标驱动解析抽象亚甲基重复：{detail}。")

    if match_index is not None and 0 <= match_index < len(step0.matches):
        match = step0.matches[match_index]
    else:
        match = default_match
    if match is None:
        return ReverseAssemblyResult(
            ok=False,
            notes=notes + ["无可用拆分候选。"],
            target_smiles=target_smiles,
            product_scaffold_line=product_scaffold_line,
        )

    notes.append(
        f"选用候选 #{match.index + 1} [{match.source}]；"
        f"键级{'✓' if match.bond_order_ok else '×'}；"
        f"扣后结构{'✓' if match.peel_topology_ok else '×'}；"
        f"组装回产物{'✓' if match.roundtrip_ok else '×'}。"
    )
    if match.roundtrip_note:
        notes.append(f"组装回产物校验：{match.roundtrip_note}")
    notes.extend(match.label_side_relation_notes)

    peeled = list(match.peeled_substituents)
    dup_peel = check_peeled_duplicate_label_substituents(match.anchors, peeled)
    if dup_peel.warn:
        notes.append(dup_peel.note)
    dup_tpl = template_attachment_duplicate_labels(product_scaffold_line)
    sym = template_attachment_label_symmetry(product_scaffold_line)
    r_groups = _assignments_from_peeled(peeled, render_images=render_images)
    frag_lines = list(match.assembly_fragment_lines)

    from chemistry.ring_transfer import materialize_transferred_ring_slots

    reactant_rows = []
    for i, tpl in enumerate(reactant_templates):
        transferred = materialize_transferred_ring_slots(
            selected_repeat.template_esmiles, tpl, match.anchors
        )
        row = assemble_reactant_from_substituents(
            transferred.template_line,
            peeled,
            render_images=render_images,
            index=i,
        )
        row.template_line = tpl
        row.messages = list(transferred.messages) + row.messages
        if transferred.unresolved_labels:
            row.success = False
            row.partial = True
            row.missing_labels = list(dict.fromkeys(
                [*row.missing_labels, *transferred.unresolved_labels]
            ))
            row.messages.insert(
                0,
                "未消费环位置标签: " + ", ".join(transferred.unresolved_labels),
            )
        reactant_rows.append(row)

    all_ok = bool(r_groups) and all(r.success for r in reactant_rows)
    if not r_groups:
        notes.append("未扣出非 H 取代基。")
    for row in reactant_rows:
        if row.success:
            notes.append(f"反应物 {row.index + 1} 组装成功: {row.assembled_smiles}")
        else:
            notes.append(
                f"反应物 {row.index + 1} 组装未完成"
                + (f"（缺 {', '.join(row.missing_labels)}）" if row.missing_labels else "")
                + f": {row.assembled_smiles or '—'}"
            )

    return ReverseAssemblyResult(
        ok=all_ok,
        notes=notes,
        target_smiles=target_smiles,
        product_scaffold_line=product_scaffold_line,
        resolved_product_scaffold_line=selected_repeat.template_esmiles,
        repeat_assignments=list(selected_repeat.repeat_assignments),
        match_index=match.index,
        match_source=match.source,
        peel_ok=match.peel_topology_ok,
        peel_topology_note=match.peel_topology_note,
        peel_topology_canonical_template=match.peel_topology_canonical_template,
        peel_topology_canonical_peeled=match.peel_topology_canonical_peeled,
        peel_topology_inchi_template=match.peel_topology_inchi_template,
        peel_topology_inchi_peeled=match.peel_topology_inchi_peeled,
        peel_topology_nonisomeric_match=match.peel_topology_nonisomeric_match,
        peel_topology_stereo_missing_hint=match.peel_topology_stereo_missing_hint,
        roundtrip_ok=match.roundtrip_ok,
        roundtrip_note=match.roundtrip_note,
        label_side_relations_ok=match.label_side_relations_ok,
        label_side_relation_notes=match.label_side_relation_notes,
        r_groups=r_groups,
        reactants=reactant_rows,
        peeled_substituents=peeled,
        assembly_fragment_lines=frag_lines,
        attachment_label_symmetry_warn=sym.warn,
        attachment_label_symmetry_groups=[list(g) for g in sym.symmetric_groups],
        attachment_label_symmetry_note=sym.note,
        attachment_duplicate_label_warn=dup_tpl.warn,
        attachment_duplicate_label_groups=list(dup_tpl.duplicate_groups),
        attachment_duplicate_label_note=dup_tpl.note,
        duplicate_label_peel_warn=dup_peel.warn,
        duplicate_label_peel_groups=list(dup_peel.mismatch_groups),
        duplicate_label_peel_note=dup_peel.note,
        template_image_base64=step0.template_image_base64,
        target_image_base64=step0.target_image_base64,
        peel_highlight_image_base64=(
            match.highlight_image_base64
            or match.labeled_image_base64
            or step0.target_image_base64
        ),
        peel_scaffold_image_base64=match.peel_scaffold_image_base64,
        peel_scaffold_smiles=match.peel_scaffold_smiles,
        peel_bond_order_ok=match.bond_order_ok,
        peel_match_count=step0.match_count,
        roundtrip_image_base64=match.assembled_image_base64,
    )


def _plain_smiles(raw: str) -> str:
    s = (raw or "").strip()
    if ESMILES_SEPARATOR in s:
        s = s.split(ESMILES_SEPARATOR, 1)[0].strip()
    return s


_COMPARE_SMILES_ALIASES = {
    "C#O": "[C-]#[O+]",
    "O#C": "[C-]#[O+]",
}


def _normalize_compare_smiles(body: str) -> str:
    b = (body or "").strip()
    return _COMPARE_SMILES_ALIASES.get(b, b)


def _mol_from_smiles_or_esmiles(smiles: str) -> Chem.Mol | None:
    body = _normalize_compare_smiles(_plain_smiles(smiles))
    if body:
        mol = Chem.MolFromSmiles(body)
        if mol is not None:
            return mol
    raw = (smiles or "").strip()
    if not raw:
        return None
    from esmiles import load_esmiles

    if ESMILES_SEPARATOR not in raw:
        raw = f"{raw}{ESMILES_SEPARATOR}"
    loaded = load_esmiles(raw, apply_dummy=True, prefer_indigo=False)
    return loaded.mol if loaded is not None else None


def structure_png_base64(smiles: str, *, render: bool) -> str:
    if not render:
        return ""
    raw = (smiles or "").strip()
    if not raw:
        return ""
    # Reverse-assembly rows are cards too: parse and depict both eSMILES and
    # plain SMILES through the one card renderer, never through Step0's Mol.
    from chemistry.rendering import render_structure

    return render_structure(raw, size=(520, 360)).image_base64


def compare_smiles_pair(assembled: str, expected: str) -> tuple[bool | None, str]:
    """Return (match_ok, note). None = cannot compare (missing / partial)."""
    exp = _normalize_compare_smiles(_plain_smiles(expected))
    got = _normalize_compare_smiles(_plain_smiles(assembled))
    if not exp:
        return None, "无标注反应物"
    if not got or "*" in got:
        return None, "组装未完成（含未连接位点）"
    if "." in got:
        return None, "组装结果为多片段混合物"
    mol_e = _mol_from_smiles_or_esmiles(expected)
    mol_g = _mol_from_smiles_or_esmiles(assembled)
    if mol_e is None:
        return None, f"无法解析标注 SMILES: {exp[:40]}"
    if mol_g is None:
        return False, f"无法解析组装 SMILES: {got[:40]}"
    from chemistry.chem_util import compare_mols_equivalence

    ok, note = compare_mols_equivalence(mol_e, mol_g)
    return ok, note


def _pair_expected_reactants(
    rows: list[ReactantAssemblyRow],
    expected_smiles: list[str],
) -> tuple[dict[int, int], dict[int, int]]:
    """Map assembled row index -> expected_smiles index.

    Returns ``(display_pairing, chemical_pairing)``. Display pairing also
    assigns leftover JSON rows to unpaired template slots in order.
    """
    if not expected_smiles:
        return {}, {}

    scored: list[tuple[int, int, int]] = []
    for ri, row in enumerate(rows):
        for ei, exp in enumerate(expected_smiles):
            if not (exp or "").strip():
                continue
            ok, _ = compare_smiles_pair(row.assembled_smiles, exp)
            if ok is True:
                score = 3
            elif ok is None:
                score = 1
            elif product_smiles_equivalent(
                _plain_smiles(row.assembled_smiles),
                _plain_smiles(exp),
            ):
                score = 2
            else:
                score = 0
            scored.append((score, ri, ei))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    pairing: dict[int, int] = {}
    used_rows: set[int] = set()
    used_expected: set[int] = set()
    for score, ri, ei in scored:
        if score == 0:
            break
        if ri in used_rows or ei in used_expected:
            continue
        pairing[ri] = ei
        used_rows.add(ri)
        used_expected.add(ei)

    chemical_pairing = dict(pairing)

    for ri in range(len(rows)):
        if ri in pairing:
            continue
        if ri < len(expected_smiles) and ri not in used_expected and (expected_smiles[ri] or "").strip():
            pairing[ri] = ri
            used_expected.add(ri)

    unpaired_rows = [ri for ri in range(len(rows)) if ri not in pairing]
    unused_expected = [
        ei
        for ei, exp in enumerate(expected_smiles)
        if ei not in used_expected and (exp or "").strip()
    ]
    for ri, ei in zip(unpaired_rows, unused_expected):
        pairing[ri] = ei
        used_expected.add(ei)

    return pairing, chemical_pairing


def attach_expected_comparisons(
    reactant_rows: list[ReactantAssemblyRow],
    expected_smiles: list[str],
    *,
    render_images: bool,
    notes: list[str],
) -> tuple[bool | None, list[dict[str, Any]]]:
    """Fill expected fields on rows; return overall compare_ok and orphan JSON rows."""
    if not expected_smiles:
        return None, []

    pairing, chemical_pairing = _pair_expected_reactants(reactant_rows, expected_smiles)
    matched_expected: set[int] = set()
    outcomes: list[bool | None] = []

    for row in reactant_rows:
        ei = pairing.get(row.index)
        exp = expected_smiles[ei] if ei is not None and ei < len(expected_smiles) else ""
        row.expected_smiles = exp
        row.expected_image_base64 = structure_png_base64(exp, render=render_images)
        if not exp:
            row.match_expected = None
            row.match_note = "无对应 JSON 标注"
            continue
        matched_expected.add(ei)
        ok, note = compare_smiles_pair(row.assembled_smiles, exp)
        row.match_expected = ok
        if ei is not None and ei != row.index:
            note = f"标注来自 JSON 反应物 #{ei + 1}（非模板顺序）" + (f"；{note}" if note else "")
        if row.index not in chemical_pairing:
            note = (
                f"按顺序展示 JSON 反应物 #{ei + 1}（模板槽位化学配对未命中）"
                + (f"；{note}" if note else "")
            )
        row.match_note = note
        outcomes.append(ok)
        label = f"反应物 {row.index + 1}"
        if ok is False:
            notes.append(f"{label} 与标注不符：{note}")
        elif ok is True and "前后不一致" in note:
            notes.append(f"{label} 连接性一致，但双键 / 标注立体与 JSON 前后不一致")
        elif ok is None:
            notes.append(f"{label} 暂无法与标注比对：{note}")

    unmatched_json: list[dict[str, Any]] = []
    for ei, exp in enumerate(expected_smiles):
        if not (exp or "").strip() or ei in matched_expected:
            continue
        unmatched_json.append(
            {
                "json_index": ei,
                "smiles": exp,
                "image_base64": structure_png_base64(exp, render=render_images),
                "note": "未匹配到任一组装反应物槽",
            }
        )
        notes.append(f"JSON 反应物 #{ei + 1} 未匹配到组装结果，标注为 {exp[:40]}")
        outcomes.append(False)

    if not outcomes:
        return None, unmatched_json
    if all(x is True for x in outcomes):
        return True, unmatched_json
    if any(x is False for x in outcomes):
        return False, unmatched_json
    return None, unmatched_json


def product_smiles_equivalent(smi_a: str, smi_b: str) -> bool:
    """Chemical match when raw SMILES strings differ."""
    a = _normalize_compare_smiles(_plain_smiles(smi_a))
    b = _normalize_compare_smiles(_plain_smiles(smi_b))
    if not a or not b:
        return False
    if a == b:
        return True
    from chemistry.chem_util import compare_mols_equivalence

    ma = Chem.MolFromSmiles(a)
    mb = Chem.MolFromSmiles(b)
    if ma is None or mb is None:
        return False
    ok, _ = compare_mols_equivalence(ma, mb)
    return ok
