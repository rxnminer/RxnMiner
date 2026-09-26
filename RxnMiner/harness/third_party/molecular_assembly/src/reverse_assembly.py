"""Reverse assembly: peel R-groups from product, fill reactant Markush templates."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from rdkit import Chem

from assembly import (
    assemble_fragments,
    mol_to_png_base64,
    mol_to_png_with_atom_notes_base64,
    normalize_target_smiles,
)
from abbrev_expand import expand_reactant_template_lines
from candidates import (
    candidate_full_pass,
    candidate_roundtrip_pass,
    candidate_sort_key,
)
from decompose import step0_scaffold_map
from esmiles_ingress import normalize_fragment_esmiles
from esmiles_loader import ESMILES_SEPARATOR, load_esmiles, parse_fragment_line
from models import MatchCandidate
from peel_topology import (
    AttachmentDuplicateLabelResult,
    AttachmentLabelSymmetryResult,
    check_attachment_label_symmetry,
    check_peeled_duplicate_label_substituents,
    check_template_duplicate_attachment_labels,
    template_mol_from_smiles,
)
from r_groups import parse_tag_string, short_display_from_source

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
    match_index: int = -1
    match_source: str = ""
    peel_ok: bool = False
    roundtrip_ok: bool = False
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

    from anchors import anchors_from_esmiles, parse_anchors, template_dummy_to_rlabel

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
                from esmiles_encode import encode_substituent_esmiles

                return encode_substituent_esmiles(mol, {dummies[0]: target_label})
        return smiles
    new_tags = _TAG_REPLACE.sub(
        lambda m: f"{m.group(1)}{target_label}{m.group(3)}",
        tags,
    )
    return f"{smiles}{ESMILES_SEPARATOR}{new_tags}"


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
        new_tags = [
            (pos, ordered_tpls[i])
            for i, (pos, _) in enumerate(sorted(tag_list, key=lambda x: x[0]))
        ]
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
) -> int:
    count = 0
    for i, tpl in enumerate(reactant_templates):
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
            img = _mol_image(smi_line, render=True)
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
    img = _mol_image(asm.smiles, render=render_images) if asm.smiles else ""

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

    step0 = step0_scaffold_map(
        target_smiles,
        product_scaffold_line,
        render_images=render_images,
    )
    notes.extend(step0.messages[:6])
    if not step0.ok or not step0.matches:
        peel_reason = next(
            (m for m in step0.messages if m),
            "未找到有效骨架嵌入。",
        )
        return ReverseAssemblyResult(
            ok=False,
            notes=notes + [f"产物拆分失败：{peel_reason}"],
            target_smiles=target_smiles,
            product_scaffold_line=product_scaffold_line,
        )

    if match_index is not None and 0 <= match_index < len(step0.matches):
        match = step0.matches[match_index]
    else:
        match = _pick_best_match(step0.matches, reactant_templates)
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

    peeled = list(match.peeled_substituents)
    dup_peel = check_peeled_duplicate_label_substituents(match.anchors, peeled)
    if dup_peel.warn:
        notes.append(dup_peel.note)
    dup_tpl = template_attachment_duplicate_labels(product_scaffold_line)
    sym = template_attachment_label_symmetry(product_scaffold_line)
    r_groups = _assignments_from_peeled(peeled, render_images=render_images)
    frag_lines = list(match.assembly_fragment_lines)

    reactant_rows = [
        assemble_reactant_from_substituents(
            tpl,
            peeled,
            render_images=render_images,
            index=i,
        )
        for i, tpl in enumerate(reactant_templates)
    ]

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
        match_index=match.index,
        match_source=match.source,
        peel_ok=match.peel_topology_ok,
        roundtrip_ok=match.roundtrip_ok,
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
    from esmiles_loader import load_esmiles

    if ESMILES_SEPARATOR not in raw:
        raw = f"{raw}{ESMILES_SEPARATOR}"
    loaded = load_esmiles(raw, apply_dummy=True, prefer_indigo=False)
    return loaded.mol if loaded is not None else None


def _mol_image(smiles: str, *, render: bool) -> str:
    if not render:
        return ""
    raw = (smiles or "").strip()
    if not raw:
        return ""
    if ESMILES_SEPARATOR in raw:
        loaded = load_esmiles(raw, apply_dummy=True, prefer_indigo=False)
        if loaded is not None and loaded.mol is not None:
            from anchors import anchors_from_esmiles, template_dummy_slot_notes

            anchors = anchors_from_esmiles(loaded.mol, loaded)
            notes = template_dummy_slot_notes(anchors)
            if notes:
                b64 = mol_to_png_with_atom_notes_base64(
                    loaded.mol,
                    size=(520, 360),
                    atom_notes=notes,
                )
                if b64:
                    return b64
            return mol_to_png_base64(loaded.mol, size=(520, 360))
    mol = _mol_from_smiles_or_esmiles(raw)
    if mol is None:
        return ""
    return mol_to_png_base64(mol, size=(520, 360))


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
    from chem_util import compare_mols_equivalence

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
        row.expected_image_base64 = _mol_image(exp, render=render_images)
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
                "image_base64": _mol_image(exp, render=render_images),
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


def template_steps_from_selected_example(example: dict) -> dict[str, list[str]]:
    """Extract reactant / product eSMILES lines from one selected_example step."""
    steps = example.get("steps") or []
    if not steps:
        return {"reactants": [], "products": []}
    step = steps[0]
    from synfacts_json_cases import esmiles

    reactants = [esmiles(r) for r in (step.get("reactants") or []) if esmiles(r)]
    products = [esmiles(p) for p in (step.get("products") or []) if esmiles(p)]
    return {"reactants": reactants, "products": products}


def example_target_products(example: dict) -> list[dict]:
    """Concrete products from example root or first step."""
    from synfacts_json_cases import esmiles, example_products

    root = example_products(example.get("products") or [])
    if root:
        return root
    steps = example.get("steps") or []
    if steps:
        return example_products(steps[0].get("products") or [])
    return []


def _select_example_product_index_light(example: dict, prods: list[dict]) -> int:
    """Pick among multiple example-side products (yield text only; no peel)."""
    if len(prods) <= 1:
        return 0
    yld = normalize_yield_percent(example_yield_text(example))
    if yld:
        for i, prod in enumerate(prods):
            desc = (prod.get("description") or "").strip()
            if normalize_yield_percent(desc) == yld:
                return i
    return 0


def example_target_smiles(
    example: dict,
    data: dict | None = None,
    *,
    template_id: str | None = None,
    product_index: int | None = None,
) -> str:
    """Concrete product SMILES (fast; does not run peel)."""
    from synfacts_json_cases import esmiles

    del data, template_id  # kept for call-site compatibility
    prods = example_target_products(example)
    if not prods:
        return ""
    if product_index is not None and 0 <= product_index < len(prods):
        return esmiles(prods[product_index])
    idx = _select_example_product_index_light(example, prods)
    return esmiles(prods[idx])


def template_steps_from_template_dict(tpl: dict) -> dict[str, list[str]]:
    """Reactant/product lines from reaction_templates or selected_example (T1)."""
    steps = tpl.get("steps") or []
    if steps:
        out = template_steps_from_selected_example(tpl)
    else:
        from synfacts_json_cases import esmiles

        reactants = [esmiles(r) for r in (tpl.get("reactants") or []) if esmiles(r)]
        products = [esmiles(p) for p in (tpl.get("products") or []) if esmiles(p)]
        out = {"reactants": reactants, "products": products}
    out["reactants"] = expand_reactant_template_lines(out["reactants"])
    out["products"] = [normalize_fragment_esmiles(p) for p in out.get("products") or []]
    return out


def resolve_inherited_template(data: dict, template_id: str) -> dict | None:
    """Find template example in selected_examples by instance_id (e.g. T1)."""
    tid = (template_id or "").strip()
    if not tid:
        return None
    for ex in data.get("selected_examples") or []:
        if (ex.get("instance_id") or "").strip() == tid and not (ex.get("inherit_template") or "").strip():
            return ex
    for tpl in data.get("reaction_templates") or []:
        if (tpl.get("template_id") or "").strip() == tid:
            return tpl
    return None


def template_steps_for_id(data: dict, template_id: str) -> dict[str, list[str]]:
    """Markush reactant/product lines from page data (e.g. _no_batch T1/T2 rows)."""
    tpl = resolve_inherited_template(data, template_id)
    if tpl is None:
        return {"reactants": [], "products": []}
    return template_steps_from_template_dict(tpl)


def example_reactant_smiles_list(example: dict) -> list[str]:
    """Concrete reactants from example root or first step."""
    from synfacts_json_cases import esmiles

    root = [esmiles(r) for r in (example.get("reactants") or []) if esmiles(r)]
    if root:
        return root
    steps = example.get("steps") or []
    if steps:
        return [esmiles(r) for r in (steps[0].get("reactants") or []) if esmiles(r)]
    return []


def example_yield_text(example: dict) -> str:
    for m in example.get("metrics") or []:
        if (m.get("role") or "").strip() == "yield":
            return (m.get("text") or "").strip()
    prods = example_target_products(example)
    if prods:
        desc = (prods[0].get("description") or "").strip()
        if desc and ("%" in desc or "yield" in desc.lower()):
            return desc
    return ""


_YIELD_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def normalize_yield_percent(text: str) -> str | None:
    """Extract comparable yield token, e.g. ``63% yield in PhMe`` → ``63%``."""
    if not text:
        return None
    m = _YIELD_PCT.search(text.strip())
    if not m:
        return None
    val = m.group(1)
    if "." in val:
        return f"{float(val):g}%"
    return f"{val}%"


def example_yields_equivalent(ex_a: dict, ex_b: dict) -> bool:
    ya = normalize_yield_percent(example_yield_text(ex_a))
    yb = normalize_yield_percent(example_yield_text(ex_b))
    return bool(ya and yb and ya == yb)


def product_smiles_equivalent(smi_a: str, smi_b: str) -> bool:
    """Chemical match when raw SMILES strings differ."""
    a = _normalize_compare_smiles(_plain_smiles(smi_a))
    b = _normalize_compare_smiles(_plain_smiles(smi_b))
    if not a or not b:
        return False
    if a == b:
        return True
    from chem_util import compare_mols_equivalence

    ma = Chem.MolFromSmiles(a)
    mb = Chem.MolFromSmiles(b)
    if ma is None or mb is None:
        return False
    ok, _ = compare_mols_equivalence(ma, mb)
    return ok


def _refine_official_matches(
    matches: list[dict],
    source_example: dict,
) -> list[dict]:
    """Narrow candidates by yield and instance_id when those fields help."""
    yld = example_yield_text(source_example)
    instance_id = (source_example.get("instance_id") or "").strip()

    if yld:
        yield_matches = [ex for ex in matches if example_yields_equivalent(ex, source_example)]
        if yield_matches:
            matches = yield_matches
    if instance_id:
        inst_matches = [
            ex for ex in matches if (ex.get("instance_id") or "").strip() == instance_id
        ]
        if inst_matches:
            matches = inst_matches
    return matches


def _bbox_list(item: dict) -> list[float] | None:
    bb = item.get("bbox")
    if not bb or not isinstance(bb, (list, tuple)) or len(bb) < 4:
        return None
    return [float(x) for x in bb[:4]]


def _bbox_is_normalized(bbox: list[float]) -> bool:
    return all(0 <= v <= 1.5 for v in bbox)


def example_product_bbox(
    example: dict,
    data: dict | None = None,
    *,
    product_index: int | None = None,
) -> list[float] | None:
    del data
    prods = example_target_products(example)
    if not prods:
        return None
    if product_index is None:
        product_index = _select_example_product_index_light(example, prods)
    idx = product_index if 0 <= product_index < len(prods) else 0
    return _bbox_list(prods[idx])


def _example_skip_reason(ex: dict) -> str:
    inherit = (ex.get("inherit_template") or "").strip()
    instance_id = (ex.get("instance_id") or "").strip()
    if not inherit:
        label = instance_id or "模板"
        return f"模板行（{label}），不参与逆向组装"
    if not example_target_smiles(ex):
        return "无产物 SMILES，已跳过"
    return ""


def iter_example_list_rows(data: dict) -> list[dict[str, Any]]:
    """All selected_examples for UI, including skipped template / empty rows."""
    rows: list[dict[str, Any]] = []
    for i, ex in enumerate(data.get("selected_examples") or []):
        inherit = (ex.get("inherit_template") or "").strip()
        target = example_target_smiles(ex)
        skip_reason = _example_skip_reason(ex)
        reactants = example_reactant_smiles_list(ex) if inherit and target else []
        bbox = example_product_bbox(ex) if inherit and target else None
        rows.append(
            {
                "example_index": i,
                "inherit_template": inherit,
                "yield_text": example_yield_text(ex),
                "target_smiles": target or "",
                "reactant_smiles": reactants,
                "product_bbox": bbox,
                "bbox_normalized": _bbox_is_normalized(bbox) if bbox else False,
                "instance_id": (ex.get("instance_id") or "").strip(),
                "has_annotated_reactants": bool(reactants),
                "included": not bool(skip_reason),
                "skip_reason": skip_reason,
            }
        )
    return rows


def iter_reverse_example_summaries(data: dict) -> list[dict[str, Any]]:
    """One row per inheriting selected_example (keyed by yield for UI)."""
    return [row for row in iter_example_list_rows(data) if row["included"]]


def matching_official_example(
    source_data: dict,
    source_example: dict,
    official_data: dict,
    *,
    example_index: int,
) -> dict | None:
    """Map a _no_batch selected_example to the paired official JSON row."""
    official_examples = official_data.get("selected_examples") or []
    target = example_target_smiles(source_example)
    yld = example_yield_text(source_example)

    matches = [ex for ex in official_examples if example_target_smiles(ex) == target]
    matches = _refine_official_matches(matches, source_example)
    if matches:
        return matches[0]

    if target:
        matches = [
            ex
            for ex in official_examples
            if product_smiles_equivalent(example_target_smiles(ex), target)
        ]
        matches = _refine_official_matches(matches, source_example)
        if matches:
            return matches[0]

    if yld:
        matches = [
            ex for ex in official_examples if example_yields_equivalent(ex, source_example)
        ]
        instance_id = (source_example.get("instance_id") or "").strip()
        if instance_id:
            inst_matches = [
                ex for ex in matches if (ex.get("instance_id") or "").strip() == instance_id
            ]
            if inst_matches:
                matches = inst_matches
        if len(matches) > 1 and target:
            chem = [
                ex
                for ex in matches
                if product_smiles_equivalent(example_target_smiles(ex), target)
            ]
            if chem:
                return chem[0]
        if matches:
            return matches[0]

    source_rows = iter_reverse_example_summaries(source_data)
    official_rows = iter_reverse_example_summaries(official_data)
    ordinal = next(
        (i for i, row in enumerate(source_rows) if row["example_index"] == example_index),
        None,
    )
    if ordinal is not None and ordinal < len(official_rows):
        official_idx = official_rows[ordinal]["example_index"]
        if official_idx < len(official_examples):
            return official_examples[official_idx]
    return None


def reverse_assemble_from_example(
    data: dict,
    example: dict,
    *,
    render_images: bool = True,
    match_index: int | None = None,
) -> ReverseAssemblyResult:
    """Run reverse assembly for one selected_example inheriting a template."""
    inherit = (example.get("inherit_template") or "").strip()
    if not inherit:
        return ReverseAssemblyResult(ok=False, notes=["实施例未标注 inherit_template。"])

    tpl_steps = template_steps_for_id(data, inherit)
    product_scaffold = (tpl_steps["products"] or [""])[0]
    reactant_templates = tpl_steps["reactants"]
    if not product_scaffold or not reactant_templates:
        return ReverseAssemblyResult(
            ok=False,
            notes=[f"模板 {inherit} 缺少产物或反应物 Markush 行。"],
            template_id=inherit,
        )

    target = example_target_smiles(example)
    if not target:
        return ReverseAssemblyResult(ok=False, notes=["实施例无产物 SMILES。"])

    expected = example_reactant_smiles_list(example)
    markush = dict(example.get("markush_enumeration") or {})
    yield_text = example_yield_text(example)
    sym = template_attachment_label_symmetry(product_scaffold)

    result = reverse_assemble(
        target,
        product_scaffold,
        reactant_templates,
        render_images=render_images,
        match_index=match_index,
        expected_reactants=expected,
    )
    result.template_id = inherit
    result.yield_text = yield_text
    result.markush_enumeration = markush
    result.attachment_label_symmetry_warn = sym.warn
    result.attachment_label_symmetry_groups = [list(g) for g in sym.symmetric_groups]
    result.attachment_label_symmetry_note = sym.note

    if expected:
        result.compare_ok, result.unmatched_json_reactants = attach_expected_comparisons(
            result.reactants,
            expected,
            render_images=render_images,
            notes=result.notes,
        )
        if result.compare_ok is True and not any(
            "前后不一致" in (r.match_note or "") for r in result.reactants
        ):
            result.notes.append("全部反应物与 JSON 标注一致。")
    return result


def report_status_label(
    *,
    included: bool,
    skip_reason: str,
    assembly_ok: bool | None,
    compare_ok: bool | None,
    stereo_warn: bool,
) -> tuple[str, str]:
    if not included:
        return "跳过", skip_reason
    if assembly_ok is False:
        return "组装失败", "产物拆分或反应物组装未完成"
    if compare_ok is True and stereo_warn:
        return "立体不一致", "连接性一致，双键 / 标注立体与 JSON 前后不一致"
    if compare_ok is True:
        return "一致", "与正式 JSON 标注一致"
    if compare_ok is False:
        return "标注不符", "组装结果与正式 JSON 标注不一致"
    if assembly_ok is True:
        return "无法比对", "组装完成但缺少正式 JSON 标注反应物"
    return "未知", ""


def classify_failure_category(
    *,
    included: bool,
    skip_reason: str,
    notes: list[str],
    assembly_ok: bool | None,
    compare_ok: bool | None,
    stereo_warn: bool,
    has_expected: bool,
    missing_inputs: bool = False,
) -> str:
    if not included:
        return "skipped"
    if missing_inputs:
        return "missing_template"
    text = " ".join(notes)
    if any(k in text for k in ("产物 SMILES 为空", "缺少模板", "缺少产物")):
        return "missing_inputs"
    recovered_sep = "已仅用 SMILES 主体" in text
    if not recovered_sep and any(k in text for k in ("含 <sep>", "fragment 后缀", "非法后缀")):
        if any(k in text for k in ("无法解析", "产物拆分失败", "语法非法")):
            return "target_has_sep"
    if any(k in text for k in ("未展开连接位点", "语法非法", "无法被 RDKit 解析", "无法解析完整分子")):
        return "target_smiles_invalid"
    if assembly_ok is False:
        if "产物拆分失败" in text or "未找到有效骨架" in text:
            return "step0_fail"
        if "未扣出" in text or "组装未完成" in text:
            return "reactant_fail"
        return "assembly_fail"
    if compare_ok is False:
        return "compare_fail"
    if stereo_warn:
        return "stereo_warn"
    if assembly_ok is True and not has_expected:
        return "no_official_reactants"
    if assembly_ok is True and compare_ok is True:
        return "ok"
    return "unknown"


def hydrate_reverse_result_images(payload: dict[str, Any]) -> dict[str, Any]:
    """Fill empty ``image_base64`` fields from SMILES (for cache served on demand)."""
    rev = dict(payload)

    r_groups: list[dict[str, Any]] = []
    for item in rev.get("r_groups") or []:
        g = dict(item)
        if not g.get("image_base64"):
            g["image_base64"] = _mol_image(g.get("smiles_line") or "", render=True)
        r_groups.append(g)
    rev["r_groups"] = r_groups

    reactants: list[dict[str, Any]] = []
    for item in rev.get("reactants") or []:
        r = dict(item)
        if not r.get("image_base64"):
            r["image_base64"] = _mol_image(r.get("assembled_smiles") or "", render=True)
        if not r.get("expected_image_base64"):
            r["expected_image_base64"] = _mol_image(r.get("expected_smiles") or "", render=True)
        reactants.append(r)
    rev["reactants"] = reactants

    unmatched: list[dict[str, Any]] = []
    for item in rev.get("unmatched_json_reactants") or []:
        u = dict(item)
        if not u.get("image_base64"):
            u["image_base64"] = _mol_image(u.get("smiles") or "", render=True)
        unmatched.append(u)
    rev["unmatched_json_reactants"] = unmatched

    return rev


def reverse_result_to_dict(result: ReverseAssemblyResult) -> dict[str, Any]:
    from dataclasses import asdict

    return {
        "ok": result.ok,
        "notes": list(result.notes),
        "target_smiles": result.target_smiles,
        "product_scaffold_line": result.product_scaffold_line,
        "match_index": result.match_index,
        "match_source": result.match_source,
        "peel_ok": result.peel_ok,
        "roundtrip_ok": result.roundtrip_ok,
        "r_groups": [asdict(r) for r in result.r_groups],
        "reactants": [asdict(r) for r in result.reactants],
        "peeled_substituents": list(result.peeled_substituents),
        "assembly_fragment_lines": list(result.assembly_fragment_lines),
        "yield_text": result.yield_text,
        "template_id": result.template_id,
        "markush_enumeration": dict(result.markush_enumeration),
        "compare_ok": result.compare_ok,
        "unmatched_json_reactants": list(result.unmatched_json_reactants),
        **attachment_label_symmetry_payload(result.product_scaffold_line),
        **attachment_duplicate_label_payload(result.product_scaffold_line),
        "duplicate_label_peel_warn": result.duplicate_label_peel_warn,
        "duplicate_label_peel_groups": list(result.duplicate_label_peel_groups),
        "duplicate_label_peel_note": result.duplicate_label_peel_note,
    }


def pick_detail_from_notes(notes: list[str], *, list_note: str = "") -> str:
    if list_note:
        return list_note
    for note in reversed(notes):
        if not note:
            continue
        if note.startswith("产物拆分失败："):
            return note[len("产物拆分失败：") :]
        if "无法解析" in note or "含 <sep>" in note or "fragment 后缀" in note:
            return note
        if "组装未完成" in note or "未扣出" in note:
            return note
    return notes[-1][:160] if notes else ""
