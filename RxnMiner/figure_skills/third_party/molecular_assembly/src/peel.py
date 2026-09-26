"""Peel substituents by connected components; build peeled scaffold."""
from __future__ import annotations

from typing import Any

from rdkit import Chem

from align import template_heavy_embeds
from anchors import (
    AnchorInfo,
    dummy_attachment_bond_type,
    anchor_connection_bond_type,
    outside_anchor_neighbors,
    template_anchor_indices,
)
from assembly import (
    mol_to_png_base64,
    mol_to_png_labeled_base64,
    mol_to_png_peel_scaffold_base64,
    peel_scaffold_smiles_for_display,
)
from esmiles_encode import encode_substituent_esmiles
from models import DummySite, H_SUBST_SMILES, PeeledSubstituent, PeelResult, PEEL_FRAGMENT_SANITIZE, SANITIZE_RELAXED
from r_groups import group_display_label, placeholder_isotope_from_r_label
from scaffold_geom import (
    build_scaffold_with_dummies,
    collect_chiral_frame_snapshots,
    verify_chiral_frames_preserved,
)


def non_core_components(target_mol: Chem.Mol, core_full: set[int]) -> list[set[int]]:
    """Connected heavy-atom components outside mapped scaffold core."""
    seen_glob: set[int] = set()
    comps: list[set[int]] = []
    for atom in target_mol.GetAtoms():
        idx = atom.GetIdx()
        if idx in core_full or idx in seen_glob or atom.GetAtomicNum() <= 1:
            continue
        stack = [idx]
        comp: set[int] = set()
        while stack:
            i = stack.pop()
            if i in core_full or i in seen_glob or i in comp:
                continue
            a = target_mol.GetAtomWithIdx(i)
            if a.GetAtomicNum() <= 1:
                continue
            seen_glob.add(i)
            comp.add(i)
            for n in a.GetNeighbors():
                if n.GetAtomicNum() > 1:
                    stack.append(n.GetIdx())
        if comp:
            comps.append(comp)
    return comps


def _non_core_neighbor_roots(
    target_mol: Chem.Mol, anchor_full_idx: int, core_full: set[int]
) -> list[int]:
    return sorted(
        n.GetIdx()
        for n in target_mol.GetAtomWithIdx(anchor_full_idx).GetNeighbors()
        if n.GetAtomicNum() > 1 and n.GetIdx() not in core_full
    )


def compute_subst_roots(
    target_mol: Chem.Mol,
    core_full: set[int],
    anchors: list[AnchorInfo],
    template_to_full: dict[int, int],
) -> dict[str, int]:
    """Map each anchor slot → substituent root heavy atom; ``-1`` means implicit H.

    When several template R slots share one mapped anchor but the full molecule has
    fewer outside heavy neighbors, the missing slots are hydrogens. Assign the
    observed heavy substituents to the later slots deterministically so a single
    component is not later attributed to every same-anchor slot by fallback.
    """
    by_anchor: dict[int, list[AnchorInfo]] = {}
    for a in anchors:
        for tidx in template_anchor_indices(a):
            fidx = template_to_full.get(tidx, -1)
            if fidx < 0:
                continue
            by_anchor.setdefault(fidx, []).append(a)

    roots: dict[str, int] = {}
    for fidx, group in by_anchor.items():
        neighbors = _non_core_neighbor_roots(target_mol, fidx, core_full)
        slots = sorted(group, key=lambda x: x.template_dummy_idx)
        if len(slots) == len(neighbors):
            for slot, root in zip(slots, neighbors):
                roots[slot.internal_id] = root
        elif 0 < len(neighbors) < len(slots):
            n_h = len(slots) - len(neighbors)
            for slot in slots[:n_h]:
                roots[slot.internal_id] = -1
            for slot, root in zip(slots[n_h:], neighbors):
                roots[slot.internal_id] = root
        elif len(slots) == 1 and neighbors:
            roots[slots[0].internal_id] = neighbors[0]
    return roots


def anchors_touching_component(
    target_mol: Chem.Mol,
    comp: set[int],
    core_full: set[int],
    anchors: list[AnchorInfo],
    template_to_full: dict[int, int],
    *,
    subst_roots: dict[str, int] | None = None,
) -> list[AnchorInfo]:
    subst_roots = subst_roots or {}
    touching: list[AnchorInfo] = []
    for a in anchors:
        root = subst_roots.get(a.internal_id)
        if root is not None:
            if root in comp:
                touching.append(a)
            continue
        matched = False
        for tidx in template_anchor_indices(a):
            fidx = template_to_full.get(tidx, -1)
            if fidx < 0:
                continue
            for n in target_mol.GetAtomWithIdx(fidx).GetNeighbors():
                if n.GetIdx() in comp:
                    touching.append(a)
                    matched = True
                    break
            if matched:
                break
    return touching


def _mapped_anchor_fulls(
    anchor: AnchorInfo,
    template_to_full: dict[int, int],
) -> list[int]:
    return [
        template_to_full[tidx]
        for tidx in template_anchor_indices(anchor)
        if template_to_full.get(tidx, -1) >= 0
    ]


def _component_root_for_full_anchor(
    target_mol: Chem.Mol,
    anchor_full_idx: int,
    remove_set: set[int],
) -> int:
    for n in target_mol.GetAtomWithIdx(anchor_full_idx).GetNeighbors():
        if n.GetAtomicNum() > 1 and n.GetIdx() in remove_set:
            return n.GetIdx()
    return -1


def _dummy_site_bridge_key(anchor: AnchorInfo) -> str:
    if anchor.anchor_kind == "bridge":
        return anchor.internal_id
    if anchor.anchor_kind == "virtual_arc":
        return f"virtual_arc:{anchor.virtual_arc_index}:{anchor.pair_key}"
    return ""


def path_via_non_core(
    target_mol: Chem.Mol,
    start: int,
    end: int,
    core_full: set[int],
) -> bool:
    """BFS: is there a path start→end using at least one heavy atom not in core?"""
    if start == end:
        return False
    stack = [(start, False)]
    seen = {(start, False)}
    while stack:
        idx, left_core = stack.pop()
        if idx == end and left_core:
            return True
        for n in target_mol.GetAtomWithIdx(idx).GetNeighbors():
            if n.GetAtomicNum() <= 1:
                continue
            j = n.GetIdx()
            nc = left_core or (j not in core_full)
            st = (j, nc)
            if st in seen:
                continue
            seen.add(st)
            stack.append(st)
    return False


def anchors_bridged_by_substituent(
    target_mol: Chem.Mol,
    core_full: set[int],
    anchor_full_idxs: list[int],
    removed: set[int],
) -> bool:
    """Any pair of anchors still connected through atoms not in core and not removed?"""
    allowed = {
        i
        for i in range(target_mol.GetNumAtoms())
        if i not in core_full and i not in removed
    }

    def path_outside_core(a: int, b: int) -> bool:
        stack = [a]
        seen = {a}
        while stack:
            idx = stack.pop()
            if idx == b:
                return True
            for n in target_mol.GetAtomWithIdx(idx).GetNeighbors():
                if n.GetAtomicNum() <= 1:
                    continue
                j = n.GetIdx()
                if j not in allowed or j in seen:
                    continue
                seen.add(j)
                stack.append(j)
        return False

    unique_anchors = list(dict.fromkeys(anchor_full_idxs))
    for i, ai in enumerate(unique_anchors):
        for aj in unique_anchors[i + 1 :]:
            if ai == aj:
                continue
            if path_outside_core(ai, aj):
                return True
    return False


def removal_mol(target_mol: Chem.Mol, remove_idxs: set[int]) -> Chem.Mol | None:
    if not remove_idxs:
        return Chem.Mol(target_mol)
    rw = Chem.RWMol(Chem.Mol(target_mol))
    for idx in sorted(remove_idxs, reverse=True):
        if idx < rw.GetNumAtoms():
            rw.RemoveAtom(idx)
    try:
        out = rw.GetMol()
        Chem.SanitizeMol(out)
        return out
    except Exception:
        return None


def peel_embed_ok(
    target_mol: Chem.Mol,
    remove_idxs: set[int],
    template_mol: Chem.Mol,
) -> bool:
    rem = removal_mol(target_mol, remove_idxs)
    return rem is not None and template_heavy_embeds(rem, template_mol)


def peel_bridge_ok(
    target_mol: Chem.Mol,
    remove_idxs: set[int],
    core_full: set[int],
    anchor_full_idxs: list[int],
) -> bool:
    if not anchor_full_idxs:
        return True
    return not anchors_bridged_by_substituent(
        target_mol, core_full, anchor_full_idxs, remove_idxs
    )


def verify_peel_removal(
    target_mol: Chem.Mol,
    remove_idxs: set[int],
    template_mol: Chem.Mol,
    core_full: set[int],
    anchor_full_idxs: list[int],
) -> bool:
    if not peel_embed_ok(target_mol, remove_idxs, template_mol):
        return False
    return peel_bridge_ok(target_mol, remove_idxs, core_full, anchor_full_idxs)


def boundary_bond_ids(
    target_mol: Chem.Mol,
    core_full: set[int],
    comp: set[int],
) -> list[int]:
    bids: list[int] = []
    for idx in comp:
        for n in target_mol.GetAtomWithIdx(idx).GetNeighbors():
            j = n.GetIdx()
            if j in core_full:
                b = target_mol.GetBondBetweenAtoms(idx, j)
                if b is not None:
                    bids.append(b.GetIdx())
    return list(set(bids))


def stamp_peel_slots_on_boundary(
    target_mol: Chem.Mol,
    core_full: set[int],
    remove_set: set[int],
    touching: list[AnchorInfo],
    template_to_full: dict[int, int],
) -> Chem.Mol:
    """Mark comp-side boundary atoms with _peel_slot for per-* drawing labels."""
    rw = Chem.RWMol(Chem.Mol(target_mol))
    for a in touching:
        for fidx in _mapped_anchor_fulls(a, template_to_full):
            for n in rw.GetAtomWithIdx(fidx).GetNeighbors():
                leaf = n.GetIdx()
                if leaf in remove_set and leaf not in core_full:
                    leaf_atom = rw.GetAtomWithIdx(leaf)
                    leaf_atom.SetProp("_peel_slot", a.internal_id)
                    leaf_atom.SetProp("_peel_rlabel", a.r_label)
    return rw.GetMol()


SANITIZE_RELAXED = (
    Chem.SanitizeFlags.SANITIZE_ALL ^ Chem.SanitizeFlags.SANITIZE_KEKULIZE
)


def _clear_aromatic_flags(mol: Chem.Mol) -> None:
    for b in mol.GetBonds():
        b.SetIsAromatic(False)
    for a in mol.GetAtoms():
        a.SetIsAromatic(False)


def mol_copy_kekulized_for_cut(mol: Chem.Mol) -> Chem.Mol:
    """Kekulé copy so FragmentOnBonds pieces are not stuck with broken aromatic flags."""
    m = Chem.Mol(mol)
    try:
        Chem.Kekulize(m, clearAromaticFlags=True)
    except Exception:
        _clear_aromatic_flags(m)
    return m


def sanitize_peeled_piece(piece: Chem.Mol) -> Chem.Mol | None:
    """Best-effort sanitize on a cut fragment (no full aromaticity requirement)."""
    m = Chem.Mol(piece)
    for ops in (PEEL_FRAGMENT_SANITIZE, SANITIZE_RELAXED):
        try:
            Chem.SanitizeMol(m, sanitizeOps=ops)
            return m
        except Exception:
            m = Chem.Mol(piece)
            continue
    _clear_aromatic_flags(m)
    try:
        Chem.SanitizeMol(
            m,
            sanitizeOps=PEEL_FRAGMENT_SANITIZE,
        )
        return m
    except Exception:
        return m


def dummy_labels_on_substituent_piece(piece: Chem.Mol) -> dict[int, str]:
    """Map each * in peeled fragment to slot id via _peel_slot on its heavy neighbor."""
    labels: dict[int, str] = {}
    for a in piece.GetAtoms():
        if a.GetAtomicNum() != 0:
            continue
        di = a.GetIdx()
        for n in a.GetNeighbors():
            if n.GetAtomicNum() > 1 and n.HasProp("_peel_slot"):
                labels[di] = n.GetProp("_peel_slot")
                break
    return labels


def dummy_rlabels_on_substituent_piece(piece: Chem.Mol) -> dict[int, str]:
    """Map each * to upstream ``<a>`` connection label (``r_label``)."""
    out: dict[int, str] = {}
    for a in piece.GetAtoms():
        if a.GetAtomicNum() != 0:
            continue
        di = a.GetIdx()
        for n in a.GetNeighbors():
            if n.GetAtomicNum() > 1 and n.HasProp("_peel_rlabel"):
                out[di] = n.GetProp("_peel_rlabel")
                break
    return out


def _complete_bridge_dummy_rlabels(
    piece: Chem.Mol,
    dummy_rlabels: dict[int, str],
    touching: list[AnchorInfo] | None,
) -> dict[int, str]:
    """Fill unlabeled cut dummies for a single bridge / virtual-arc substituent."""
    if not touching:
        return dummy_rlabels
    bridge_labels = {
        a.r_label
        for a in touching
        if a.anchor_kind in ("bridge", "virtual_arc") and a.r_label
    }
    if len(bridge_labels) != 1:
        return dummy_rlabels
    label = next(iter(bridge_labels))
    out = dict(dummy_rlabels)
    for atom in piece.GetAtoms():
        if atom.GetAtomicNum() == 0 and atom.GetIdx() not in out:
            out[atom.GetIdx()] = label
    return out


def _apply_dummy_semantic_isotopes(mol: Chem.Mol, dummy_to_rlabel: dict[int, str]) -> Chem.Mol:
    """Optional ``[N*]`` in SMILES body from connection label (skip plain ``R``)."""
    if not dummy_to_rlabel:
        return mol
    from r_groups import _R_PLAIN

    rw = Chem.RWMol(Chem.Mol(mol))
    for dummy_idx, r_label in dummy_to_rlabel.items():
        if dummy_idx < 0 or dummy_idx >= rw.GetNumAtoms():
            continue
        raw = (r_label or "").strip()
        if _R_PLAIN.match(raw):
            continue
        atom = rw.GetAtomWithIdx(dummy_idx)
        if atom.GetAtomicNum() != 0:
            continue
        atom.SetIsotope(placeholder_isotope_from_r_label(raw))
    return rw.GetMol()


def _mol_with_orig_idx(mol: Chem.Mol) -> Chem.Mol:
    rw = Chem.RWMol(Chem.Mol(mol))
    for a in rw.GetAtoms():
        a.SetIntProp("_origIdx", a.GetIdx())
    return rw.GetMol()


def _piece_orig_heavy_indices(piece: Chem.Mol) -> set[int]:
    """Heavy atoms in a cut fragment, as pre-cut molecule indices."""
    out: set[int] = set()
    for a in piece.GetAtoms():
        if a.GetAtomicNum() <= 1:
            continue
        if a.HasProp("_origIdx"):
            out.add(int(a.GetProp("_origIdx")))
    return out


def _piece_has_peel_rlabel(piece: Chem.Mol) -> bool:
    return any(
        a.GetAtomicNum() > 1 and a.HasProp("_peel_rlabel") for a in piece.GetAtoms()
    )


def _pick_substituent_fragment(
    pieces: tuple[Chem.Mol, ...] | list[Chem.Mol],
    comp: set[int],
) -> Chem.Mol | None:
    """Choose the cut fragment whose heavy atoms align with ``comp`` (not the complement)."""
    best: Chem.Mol | None = None
    best_key: tuple[int, int, int] | None = None
    for piece in pieces:
        if not any(a.GetAtomicNum() == 0 for a in piece.GetAtoms()):
            continue
        nh = piece.GetNumHeavyAtoms()
        if nh > len(comp) + 2:
            continue
        cleaned = sanitize_peeled_piece(piece)
        if cleaned is None:
            continue
        overlap = len(_piece_orig_heavy_indices(cleaned) & comp)
        key = (overlap, int(_piece_has_peel_rlabel(cleaned)), -nh)
        if best is None or key > best_key:
            best = cleaned
            best_key = key
    return best


def fragment_smiles_for_component(
    target_mol: Chem.Mol,
    core_full: set[int],
    comp: set[int],
    bond_ids: list[int],
    touching: list[AnchorInfo] | None = None,
    template_to_full: dict[int, int] | None = None,
) -> tuple[str | None, int, dict[int, str], Chem.Mol | None]:
    if not bond_ids:
        return None, 0, {}, None
    mol = target_mol
    if touching and template_to_full:
        mol = stamp_peel_slots_on_boundary(
            target_mol, core_full, comp, touching, template_to_full
        )
    cut_mol = mol_copy_kekulized_for_cut(_mol_with_orig_idx(mol))
    try:
        frag = Chem.FragmentOnBonds(cut_mol, bond_ids, addDummies=True)
        pieces = Chem.GetMolFrags(frag, asMols=True, sanitizeFrags=False)
    except Exception:
        return None, 0, {}, None
    best = _pick_substituent_fragment(pieces, comp)
    if best is None:
        return None, 0, {}, None
    dummy_labels = dummy_labels_on_substituent_piece(best)
    dummy_rlabels = dummy_rlabels_on_substituent_piece(best)
    dummy_rlabels = _complete_bridge_dummy_rlabels(best, dummy_rlabels, touching)
    best = _apply_dummy_semantic_isotopes(best, dummy_rlabels)
    try:
        smi = encode_substituent_esmiles(best, dummy_rlabels)
        return smi, best.GetNumHeavyAtoms(), dummy_labels, best
    except Exception:
        return None, 0, {}, None


def peeled_fragment_image_from_mol(
    mol: Chem.Mol,
    group_label: str,
    per_dummy_labels: dict[int, str] | None = None,
) -> str:
    """Draw substituent from Mol (keeps _peel_slot); group_label only for fallback."""
    try:
        labels: dict[int, str] = dict(per_dummy_labels or {})
        highlight: list[int] = []
        for a in mol.GetAtoms():
            if a.GetAtomicNum() == 0:
                idx = a.GetIdx()
                if idx not in labels:
                    labels[idx] = group_label
                highlight.append(idx)
        return mol_to_png_labeled_base64(
            mol,
            size=(300, 220),
            atom_labels=labels,
            highlight_atoms=highlight,
        )
    except Exception:
        return ""


def greedy_expand_removal(
    target_mol: Chem.Mol,
    template_mol: Chem.Mol,
    core_full: set[int],
    comp: set[int],
    anchor_full_idxs: list[int],
) -> set[int]:
    """Grow removal only when template no longer embeds after cutting the component."""
    if verify_peel_removal(target_mol, set(comp), template_mol, core_full, anchor_full_idxs):
        return set(comp)
    if peel_bridge_ok(target_mol, set(comp), core_full, anchor_full_idxs):
        return set(comp)
    best = set(comp)
    for bid in boundary_bond_ids(target_mol, core_full, comp):
        b = target_mol.GetBondWithIdx(bid)
        i, j = b.GetBeginAtomIdx(), b.GetEndAtomIdx()
        leaf = i if i in comp else (j if j in comp else -1)
        if leaf < 0:
            continue
        trial = set(comp) | substituent_atoms_bfs(target_mol, leaf, core_full)
        if verify_peel_removal(
            target_mol, trial, template_mol, core_full, anchor_full_idxs
        ):
            return trial
        if peel_embed_ok(target_mol, trial, template_mol) and len(trial) > len(best):
            best = trial
    return best


def substituent_atoms_bfs(
    target_mol: Chem.Mol,
    start_idx: int,
    core_full: set[int],
) -> set[int]:
    seen: set[int] = set()
    stack = [start_idx]
    while stack:
        idx = stack.pop()
        if idx in core_full or idx in seen:
            continue
        atom = target_mol.GetAtomWithIdx(idx)
        if atom.GetAtomicNum() <= 1:
            continue
        seen.add(idx)
        for n in atom.GetNeighbors():
            if n.GetAtomicNum() > 1:
                stack.append(n.GetIdx())
    return seen


def peel_substituents(
    target_mol: Chem.Mol,
    template_mol: Chem.Mol,
    template_to_full: dict[int, int],
    anchors: list[AnchorInfo],
    *,
    only_r_label: str | None = None,
    render_images: bool = True,
) -> PeelResult:
    """Peel by non-core connected components; verify template embed after removal."""
    messages: list[str] = []
    core_full = set(template_to_full.values())
    if not core_full:
        return PeelResult(ok=False, messages=["无有效骨架映射，无法扣取代基。"])

    substituents: list[PeeledSubstituent] = []
    all_subst: set[int] = set()
    dummy_sites: list[DummySite] = []
    dummy_label_by_site: list[tuple[str, str]] = []
    consumed_slots: set[str] = set()
    subst_roots = compute_subst_roots(
        target_mol, core_full, anchors, template_to_full
    )

    for comp in non_core_components(target_mol, core_full):
        touching = anchors_touching_component(
            target_mol,
            comp,
            core_full,
            anchors,
            template_to_full,
            subst_roots=subst_roots,
        )
        if not touching:
            continue
        slot_ids = [a.display_label for a in touching]
        disp = group_display_label([a.internal_id for a in touching])
        disp_show = (
            group_display_label([a.display_label for a in touching])
            if len(touching) > 1
            else touching[0].display_label
        )
        anchor_fulls = list(
            dict.fromkeys(
                fidx
                for a in touching
                for fidx in _mapped_anchor_fulls(a, template_to_full)
            )
        )
        remove_set = set(comp)
        if not peel_embed_ok(target_mol, remove_set, template_mol):
            expanded = greedy_expand_removal(
                target_mol, template_mol, core_full, comp, anchor_fulls
            )
            if expanded != remove_set:
                messages.append(
                    f"{disp}：整块嵌入未过，贪心扩增移除 {len(expanded)} 原子。"
                )
            remove_set = expanded
        bond_ids = boundary_bond_ids(target_mol, core_full, remove_set)
        smi, nh, dummy_labels, sub_mol = fragment_smiles_for_component(
            target_mol,
            core_full,
            remove_set,
            bond_ids,
            touching=touching,
            template_to_full=template_to_full,
        )
        if not smi or sub_mol is None:
            messages.append(f"{disp}：切断失败（{len(touching)} 锚点）。")
            continue
        if not verify_peel_removal(
            target_mol, remove_set, template_mol, core_full, anchor_fulls
        ):
            messages.append(f"{disp}：移除后模板嵌入/桥接检查未通过，仍展示。")

        all_subst |= remove_set
        for a in touching:
            for tidx in template_anchor_indices(a):
                fidx = template_to_full.get(tidx, -1)
                if fidx < 0:
                    continue
                root = _component_root_for_full_anchor(target_mol, fidx, remove_set)
                if root < 0:
                    root = subst_roots.get(a.internal_id, -1)
                if a.template_dummy_idx >= 0:
                    bt = dummy_attachment_bond_type(template_mol, a.template_dummy_idx, tidx)
                else:
                    bt = anchor_connection_bond_type(
                        template_mol,
                        a,
                        subst_neighbor_idx=root,
                    )
                site = DummySite(
                    orig_anchor=fidx,
                    orig_leaf=root,
                    bond_type=bt,
                    r_label=a.r_label,
                    display_label=a.display_label,
                    bridge_key=_dummy_site_bridge_key(a),
                )
                dummy_sites.append(site)
            dummy_label_by_site.append((a.r_label, a.display_label))
            consumed_slots.add(a.internal_id)

        src_labels = ",".join(a.r_label for a in touching)
        sub_img = (
            peeled_fragment_image_from_mol(sub_mol, disp, per_dummy_labels=dummy_labels)
            if render_images
            else ""
        )
        substituents.append(
            PeeledSubstituent(
                display_label=disp_show,
                slot_ids=slot_ids,
                anchor_full_idxs=anchor_fulls,
                smiles=smi,
                heavy_atoms=nh,
                r_label=src_labels,
                internal_id=a.internal_id if len(touching) == 1 else disp,
                anchor_full_idx=anchor_fulls[0] if anchor_fulls else -1,
                image_base64=sub_img,
            )
        )
        messages.append(
            f"{disp}：{len(touching)} 齿，{nh} 重原子；源 [{src_labels}]；SMILES {smi}"
        )

    for a in anchors:
        if a.internal_id in consumed_slots:
            continue
        if only_r_label and a.r_label != only_r_label:
            continue
        anchor_fulls = _mapped_anchor_fulls(a, template_to_full)
        if not anchor_fulls:
            continue
        root = subst_roots.get(a.internal_id)
        if root != -1:
            outside = [
                n
                for full_idx in anchor_fulls
                for n in outside_anchor_neighbors(target_mol, full_idx, core_full)
            ]
            if outside:
                continue
        for tidx in template_anchor_indices(a):
            fidx = template_to_full.get(tidx, -1)
            if fidx < 0:
                continue
            if a.template_dummy_idx >= 0:
                bt = dummy_attachment_bond_type(template_mol, a.template_dummy_idx, tidx)
            else:
                bt = anchor_connection_bond_type(template_mol, a)
            site = DummySite(
                orig_anchor=fidx,
                orig_leaf=-1,
                bond_type=bt,
                r_label=a.r_label,
                display_label=a.display_label,
                bridge_key=_dummy_site_bridge_key(a),
            )
            dummy_sites.append(site)
        dummy_label_by_site.append((a.r_label, a.display_label))
        consumed_slots.add(a.internal_id)
        h_mol = Chem.MolFromSmiles(H_SUBST_SMILES)
        h_smi = encode_substituent_esmiles(h_mol, {0: a.r_label}) if h_mol else H_SUBST_SMILES
        sub_img = ""
        if render_images and h_mol:
            sub_img = peeled_fragment_image_from_mol(
                h_mol, a.display_label, {0: a.display_label}
            )
        substituents.append(
            PeeledSubstituent(
                display_label=a.display_label,
                slot_ids=[a.internal_id],
                anchor_full_idxs=anchor_fulls,
                smiles=h_smi,
                heavy_atoms=0,
                r_label=a.r_label,
                internal_id=a.internal_id,
                anchor_full_idx=anchor_fulls[0],
                image_base64=sub_img,
            )
        )
        messages.append(f"{a.display_label}：*[H]（槽位无骨架外重原子）。")

    frame_snaps = collect_chiral_frame_snapshots(
        target_mol, core_full, all_subst, template_to_full
    )
    scaffold_mol, frame_fix_msgs = build_scaffold_with_dummies(
        target_mol,
        all_subst,
        dummy_sites,
        frame_snaps=frame_snaps,
    )
    messages.extend(frame_fix_msgs)
    parity_ok, parity_mm = verify_chiral_frames_preserved(
        target_mol, scaffold_mol, frame_snaps
    )
    if frame_snaps:
        messages.append(
            f"产物↔扣后手性帧：{len(frame_snaps)} 中心，"
            f"{'一致' if parity_ok else f'不一致 {len(parity_mm)}'}"
        )
        for m in parity_mm:
            messages.append(
                f"  full@{m['center_full']} leaving@{m.get('leaving_full', '?')}：{m['note']}"
            )

    scaffold_smi = ""
    scaffold_img = ""
    if scaffold_mol is not None:
        scaffold_smi = peel_scaffold_smiles_for_display(
            scaffold_mol,
            template_mol=template_mol,
            template_to_full=template_to_full,
        )
        if render_images:
            try:
                from peel_topology import is_attachment_placeholder
                from r_groups import placeholder_isotope_from_r_label

                labels: dict[int, str] = {}
                iso_to_disp = {
                    placeholder_isotope_from_r_label(rl): disp
                    for rl, disp in dummy_label_by_site
                }
                for atom in scaffold_mol.GetAtoms():
                    if not is_attachment_placeholder(atom):
                        continue
                    disp = iso_to_disp.get(atom.GetIsotope(), "")
                    if disp:
                        labels[atom.GetIdx()] = disp
                scaffold_img = mol_to_png_peel_scaffold_base64(
                    scaffold_mol,
                    size=(520, 360),
                    atom_notes=labels,
                )
            except Exception:
                scaffold_img = mol_to_png_base64(scaffold_mol, size=(520, 360))

    return PeelResult(
        ok=bool(substituents),
        scaffold_smiles=scaffold_smi,
        scaffold_image_base64=scaffold_img,
        scaffold_mol=scaffold_mol,
        target_labeled_image_base64="",
        substituents=substituents,
        messages=messages,
        peel_parity_ok=parity_ok,
        peel_parity_mismatches=parity_mm,
    )
