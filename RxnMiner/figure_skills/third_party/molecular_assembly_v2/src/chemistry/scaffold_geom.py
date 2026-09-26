"""2D layout, wedge/dash, and chiral-frame geometry on peeled scaffolds."""
from __future__ import annotations

from typing import Any

from rdkit import Chem
from rdkit.Chem import AllChem
from rdkit.Geometry import Point3D

from chemistry.models import ChiralFrameSnapshot, DummySite
from chemistry.peel_topology import is_attachment_placeholder, make_attachment_placeholder_atom
from chemistry.r_groups import placeholder_isotope_from_r_label
from chemistry.stereo import scaffold_orig_to_new

def bond_dir_viewed_from_anchor(
    mol: Chem.Mol, anchor_idx: int, other_idx: int
) -> Chem.BondDir:
    """Bond wedge/dash direction as drawn from the anchor atom (toward a neighbor)."""
    bond = mol.GetBondBetweenAtoms(anchor_idx, other_idx)
    if bond is None:
        return Chem.BondDir.NONE
    d = bond.GetBondDir()
    if d == Chem.BondDir.NONE:
        return Chem.BondDir.NONE
    if bond.GetBeginAtomIdx() == anchor_idx:
        return d
    flip = {
        Chem.BondDir.BEGINWEDGE: Chem.BondDir.BEGINDASH,
        Chem.BondDir.BEGINDASH: Chem.BondDir.BEGINWEDGE,
        Chem.BondDir.ENDUPRIGHT: Chem.BondDir.ENDDOWNRIGHT,
        Chem.BondDir.ENDDOWNRIGHT: Chem.BondDir.ENDUPRIGHT,
    }
    return flip.get(d, Chem.BondDir.NONE)


def anchor_bond_dirs_to_removed(
    mol: Chem.Mol, remove_idxs: set[int], anchor_idxs: set[int]
) -> dict[int, Chem.BondDir]:
    """Before peel: map anchor -> wedge/dash on the bond that will be cut to substituent."""
    dirs: dict[int, Chem.BondDir] = {}
    for anchor_idx in anchor_idxs:
        for n in mol.GetAtomWithIdx(anchor_idx).GetNeighbors():
            j = n.GetIdx()
            if j not in remove_idxs:
                continue
            dirs[anchor_idx] = bond_dir_viewed_from_anchor(mol, anchor_idx, j)
            break
    return dirs


def _setbond_dir_viewed_from_anchor(
    rw: Chem.RWMol, anchor_idx: int, other_idx: int, bd: Chem.BondDir
) -> None:
    """Apply wedge/dash on scaffold bond as seen from anchor_idx toward other_idx."""
    bond = rw.GetBondBetweenAtoms(anchor_idx, other_idx)
    if bond is None:
        return
    if bd == Chem.BondDir.NONE:
        bond.SetBondDir(Chem.BondDir.NONE)
        return
    if bond.GetBeginAtomIdx() == anchor_idx:
        bond.SetBondDir(bd)
    else:
        bond.SetBondDir(flip_bond_dir(bd))


def product_mol_with_wedge_bond_dirs(target_mol: Chem.Mol) -> Chem.Mol:
    """Copy of target with 2D coords + WedgeMolBonds so SMILES stereo becomes BondDir."""
    work = Chem.Mol(target_mol)
    ensure_2d_coords(work)
    if work.GetNumConformers() > 0:
        try:
            AllChem.WedgeMolBonds(work, work.GetConformer())
        except Exception:
            pass
    return work


def _dummy_idx_for_site(rw: Chem.RWMol, new_anchor: int, site: DummySite) -> int | None:
    want_iso = placeholder_isotope_from_r_label(site.r_label) if site.r_label else 0
    for n in rw.GetAtomWithIdx(new_anchor).GetNeighbors():
        if not is_attachment_placeholder(n):
            continue
        if site.r_label and n.GetIsotope() == want_iso:
            return n.GetIdx()
    return None


def copy_bond_dirs_from_product(
    target_mol: Chem.Mol,
    scaffold_mol: Chem.Mol,
    remove_idxs: set[int],
    anchor_orig_idxs: set[int],
    *,
    dummy_sites: list[DummySite] | None = None,
) -> Chem.Mol:
    """Copy product BondDir onto peeled scaffold (core–core + anchor→*); NONE clears."""
    src = product_mol_with_wedge_bond_dirs(target_mol)
    rw = Chem.RWMol(Chem.Mol(scaffold_mol))
    orig2new = scaffold_orig_to_new(rw)
    if not orig2new:
        return scaffold_mol

    for bond in src.GetBonds():
        i, j = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if i in remove_idxs or j in remove_idxs:
            continue
        si, sj = orig2new.get(i), orig2new.get(j)
        if si is None or sj is None:
            continue
        bd = bond_dir_viewed_from_anchor(src, i, j)
        _setbond_dir_viewed_from_anchor(rw, si, sj, bd)

    sites = dummy_sites or []
    if sites:
        for site in sites:
            new_a = orig2new.get(site.orig_anchor)
            if new_a is None:
                continue
            bd = Chem.BondDir.NONE
            if site.orig_leaf >= 0:
                bd = bond_dir_viewed_from_anchor(src, site.orig_anchor, site.orig_leaf)
            dummy_i = _dummy_idx_for_site(rw, new_a, site)
            if dummy_i is not None:
                _setbond_dir_viewed_from_anchor(rw, new_a, dummy_i, bd)
    else:
        for orig_anchor in anchor_orig_idxs:
            new_a = orig2new.get(orig_anchor)
            if new_a is None:
                continue
            bd = Chem.BondDir.NONE
            for n in src.GetAtomWithIdx(orig_anchor).GetNeighbors():
                j = n.GetIdx()
                if j in remove_idxs:
                    bd = bond_dir_viewed_from_anchor(src, orig_anchor, j)
                    break
            for n in rw.GetAtomWithIdx(new_a).GetNeighbors():
                if is_attachment_placeholder(n):
                    _setbond_dir_viewed_from_anchor(rw, new_a, n.GetIdx(), bd)
                    break

    return rw.GetMol()


def ensure_2d_coords(mol: Chem.Mol) -> None:
    """Layout for parity math only; use a copy if the mol is used for drawing."""
    if mol.GetNumConformers() == 0:
        AllChem.Compute2DCoords(mol)


def align_scaffold_2d_to_target(
    scaffold_mol: Chem.Mol,
    target_mol: Chem.Mol,
    remove_idxs: set[int],
    dummy_sites: list[DummySite],
) -> Chem.Mol:
    """Keep peeled scaffold 2D layout aligned with product (core atoms + * at old substituent sites)."""
    ensure_2d_coords(target_mol)
    orig2new = scaffold_orig_to_new(scaffold_mol)
    if not orig2new:
        return scaffold_mol
    rw = Chem.RWMol(Chem.Mol(scaffold_mol))
    if rw.GetNumConformers() == 0:
        rw.AddConformer(Chem.Conformer(rw.GetNumAtoms()), assignId=True)
    conf = rw.GetConformer()
    conf_t = target_mol.GetConformer()
    for orig, new in orig2new.items():
        if orig < target_mol.GetNumAtoms():
            conf.SetAtomPosition(new, conf_t.GetAtomPosition(orig))
    for site in dummy_sites:
        new_a = orig2new.get(site.orig_anchor)
        if new_a is None:
            continue
        dummy_i = _dummy_idx_for_site(rw, new_a, site)
        if dummy_i is None:
            continue
        placed = False
        if site.orig_leaf >= 0 and site.orig_leaf in remove_idxs:
            conf.SetAtomPosition(dummy_i, conf_t.GetAtomPosition(site.orig_leaf))
            placed = True
        if not placed:
            for n in target_mol.GetAtomWithIdx(site.orig_anchor).GetNeighbors():
                j = n.GetIdx()
                if j in remove_idxs:
                    conf.SetAtomPosition(dummy_i, conf_t.GetAtomPosition(j))
                    placed = True
                    break
        if not placed:
            pa = conf.GetAtomPosition(new_a)
            conf.SetAtomPosition(dummy_i, Point3D(pa.x + 0.75, pa.y + 0.35, 0.0))
    coord_map: dict[int, Point3D] = {}
    for i in range(rw.GetNumAtoms()):
        coord_map[i] = conf.GetAtomPosition(i)
    try:
        AllChem.Compute2DCoords(rw, coordMap=coord_map, canonOrient=False)
    except TypeError:
        pass
    return rw.GetMol()


def cross2d_z(
    ox: float, oy: float, ax: float, ay: float, bx: float, by: float
) -> float:
    return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)


def leaving_side_sign_2d(
    mol: Chem.Mol,
    center_idx: int,
    ordered_core: tuple[int, ...],
    leaving_idx: int,
) -> int | None:
    """+1 / -1: which side leaving sits vs template-ordered core frame (2D)."""
    if len(ordered_core) < 2:
        return None
    work = Chem.Mol(mol)
    ensure_2d_coords(work)
    conf = work.GetConformer()
    pc = conf.GetAtomPosition(center_idx)
    pa = conf.GetAtomPosition(ordered_core[0])
    pb = conf.GetAtomPosition(ordered_core[1])
    pl = conf.GetAtomPosition(leaving_idx)
    ref = cross2d_z(pc.x, pc.y, pa.x, pa.y, pb.x, pb.y)
    side = cross2d_z(pc.x, pc.y, pa.x, pa.y, pl.x, pl.y)
    if abs(ref) < 1e-8:
        return None
    return 1 if ref * side > 0 else -1


def collect_chiral_frame_snapshots(
    target_mol: Chem.Mol,
    core_full: set[int],
    remove_idxs: set[int],
    template_to_full: dict[int, int],
) -> list[ChiralFrameSnapshot]:
    """Before peel: record leaving-ligand side using core neighbors sorted by template_idx."""
    full_to_t = {int(v): int(k) for k, v in template_to_full.items()}
    snaps: list[ChiralFrameSnapshot] = []
    for center in sorted(core_full):
        atom = target_mol.GetAtomWithIdx(center)
        if atom.GetChiralTag() == Chem.ChiralType.CHI_UNSPECIFIED:
            continue
        core_nbrs: list[int] = []
        leaving_nbrs: list[int] = []
        for n in atom.GetNeighbors():
            j = n.GetIdx()
            if n.GetAtomicNum() <= 1:
                continue
            if j in remove_idxs:
                leaving_nbrs.append(j)
            elif j in core_full:
                core_nbrs.append(j)
        if len(leaving_nbrs) != 1 or len(core_nbrs) < 2:
            continue
        leaving = leaving_nbrs[0]
        ordered = tuple(sorted(core_nbrs, key=lambda j: full_to_t.get(j, 10**9)))
        sign = leaving_side_sign_2d(target_mol, center, ordered, leaving)
        if sign is None:
            continue
        snaps.append(
            ChiralFrameSnapshot(
                center_full=center,
                ordered_core_full=ordered,
                leaving_full=leaving,
                parity_sign=sign,
            )
        )
    return snaps


def flip_bond_dir(bd: Chem.BondDir) -> Chem.BondDir:
    flip = {
        Chem.BondDir.BEGINWEDGE: Chem.BondDir.BEGINDASH,
        Chem.BondDir.BEGINDASH: Chem.BondDir.BEGINWEDGE,
        Chem.BondDir.ENDUPRIGHT: Chem.BondDir.ENDDOWNRIGHT,
        Chem.BondDir.ENDDOWNRIGHT: Chem.BondDir.ENDUPRIGHT,
    }
    return flip.get(bd, Chem.BondDir.NONE)


def copy_core_chiral_tags_from_target(
    scaffold_mol: Chem.Mol,
    target_mol: Chem.Mol,
    snapshots: list[ChiralFrameSnapshot],
) -> Chem.Mol:
    """Re-apply tetrahedral tags from product onto peeled core atoms (_origIdx map)."""
    if not snapshots:
        return scaffold_mol
    rw = Chem.RWMol(Chem.Mol(scaffold_mol))
    orig2new = scaffold_orig_to_new(rw)
    for snap in snapshots:
        center_s = orig2new.get(snap.center_full)
        if center_s is None:
            continue
        tag = target_mol.GetAtomWithIdx(snap.center_full).GetChiralTag()
        if tag != Chem.ChiralType.CHI_UNSPECIFIED:
            rw.GetAtomWithIdx(center_s).SetChiralTag(tag)
    return rw.GetMol()


def report_chiral_frame_side_mismatches(
    scaffold_mol: Chem.Mol,
    snapshots: list[ChiralFrameSnapshot],
) -> list[str]:
    """Diagnostic only: note when * side differs from pre-peel snapshot (no BondDir edits)."""
    if not snapshots:
        return []
    orig2new = scaffold_orig_to_new(scaffold_mol)
    msgs: list[str] = []

    for snap in snapshots:
        center_s = orig2new.get(snap.center_full)
        if center_s is None:
            continue
        core_s = tuple(
            orig2new[c] for c in snap.ordered_core_full if c in orig2new
        )
        if len(core_s) < 2:
            continue
        leaving_s: int | None = None
        for n in scaffold_mol.GetAtomWithIdx(center_s).GetNeighbors():
            if is_attachment_placeholder(n):
                leaving_s = n.GetIdx()
                break
        if leaving_s is None:
            msgs.append(
                f"手性帧 full@{snap.center_full}：扣后无 *（原 leaving {snap.leaving_full}）。"
            )
            continue
        new_sign = leaving_side_sign_2d(scaffold_mol, center_s, core_s, leaving_s)
        if new_sign is None or new_sign == snap.parity_sign:
            continue
        msgs.append(
            f"手性帧 full@{snap.center_full}：产物↔扣后侧面不一致（快照 {snap.parity_sign}，"
            f"扣后 {new_sign}）；楔/虚线已从产物复制，未自动改键。"
        )
    return msgs


def verify_chiral_frames_preserved(
    target_mol: Chem.Mol,
    scaffold_mol: Chem.Mol | None,
    snapshots: list[ChiralFrameSnapshot],
) -> tuple[bool, list[dict[str, Any]]]:
    """Product (before peel) vs peeled scaffold using template-ordered core frame."""
    if scaffold_mol is None or not snapshots:
        return True, []
    orig2new = scaffold_orig_to_new(scaffold_mol)
    mismatches: list[dict[str, Any]] = []
    for snap in snapshots:
        center_s = orig2new.get(snap.center_full)
        if center_s is None:
            mismatches.append(
                {
                    "center_full": snap.center_full,
                    "note": "扣后找不到对应手性中心",
                }
            )
            continue
        core_s = tuple(
            orig2new[c] for c in snap.ordered_core_full if c in orig2new
        )
        if len(core_s) < 2:
            continue
        leaving_s: int | None = None
        for n in scaffold_mol.GetAtomWithIdx(center_s).GetNeighbors():
            if is_attachment_placeholder(n):
                leaving_s = n.GetIdx()
                break
        if leaving_s is None:
            continue
        new_sign = leaving_side_sign_2d(scaffold_mol, center_s, core_s, leaving_s)
        if new_sign is None:
            continue
        tag_t = target_mol.GetAtomWithIdx(snap.center_full).GetChiralTag()
        tag_s = scaffold_mol.GetAtomWithIdx(center_s).GetChiralTag()
        if (
            tag_t != Chem.ChiralType.CHI_UNSPECIFIED
            and tag_s != Chem.ChiralType.CHI_UNSPECIFIED
            and tag_t == tag_s
        ):
            continue
        if new_sign != snap.parity_sign:
            mismatches.append(
                {
                    "center_full": snap.center_full,
                    "leaving_full": snap.leaving_full,
                    "before_sign": snap.parity_sign,
                    "after_sign": new_sign,
                    "core_full": list(snap.ordered_core_full),
                    "note": "产物↔扣后：core 帧侧面不一致（* 相对三 core 配体）",
                }
            )
    return len(mismatches) == 0, mismatches


def apply_anchor_bond_dirs_to_dummies(mol: Chem.Mol, anchor_dirs: dict[int, Chem.BondDir]) -> Chem.Mol:
    """Set anchor->* bond dirs after sanitize (orig idx via _origIdx)."""
    if not anchor_dirs:
        return mol
    rw = Chem.RWMol(mol)
    orig2new = scaffold_orig_to_new(rw)
    for orig_anchor, bd in anchor_dirs.items():
        new_anchor = orig2new.get(orig_anchor)
        if new_anchor is None or bd == Chem.BondDir.NONE:
            continue
        for n in rw.GetAtomWithIdx(new_anchor).GetNeighbors():
            if not is_attachment_placeholder(n):
                continue
            b = rw.GetBondBetweenAtoms(new_anchor, n.GetIdx())
            if b is not None:
                b.SetBondDir(bd)
    return rw.GetMol()


def build_scaffold_with_dummies(
    target_mol: Chem.Mol,
    remove_idxs: set[int],
    dummy_sites: list[DummySite],
    *,
    frame_snaps: list[ChiralFrameSnapshot] | None = None,
) -> tuple[Chem.Mol | None, list[str]]:
    """Remove substituents; add Ts placeholders at anchors (before Sanitize)."""
    anchor_orig_idxs = {s.orig_anchor for s in dummy_sites}
    snaps = frame_snaps or []

    rw = Chem.RWMol(Chem.Mol(target_mol))
    for a in rw.GetAtoms():
        a.SetIntProp("_origIdx", a.GetIdx())
    for idx in sorted(remove_idxs, reverse=True):
        if idx < rw.GetNumAtoms():
            rw.RemoveAtom(idx)
    orig_to_new: dict[int, int] = {}
    for a in rw.GetAtoms():
        if a.HasProp("_origIdx"):
            orig_to_new[int(a.GetProp("_origIdx"))] = a.GetIdx()
    bridge_dummy_by_key: dict[str, int] = {}
    for site in dummy_sites:
        new_anchor = orig_to_new.get(site.orig_anchor)
        if new_anchor is None:
            continue
        if site.bridge_key:
            di = bridge_dummy_by_key.get(site.bridge_key)
            if di is None:
                di = rw.AddAtom(make_attachment_placeholder_atom(site.r_label))
                bridge_dummy_by_key[site.bridge_key] = di
        else:
            di = rw.AddAtom(make_attachment_placeholder_atom(site.r_label))
        if rw.GetBondBetweenAtoms(new_anchor, di) is not None:
            continue
        rw.AddBond(new_anchor, di, site.bond_type)
    out = rw.GetMol()
    try:
        Chem.SanitizeMol(out)
    except Exception:
        try:
            Chem.SanitizeMol(
                out, sanitizeOps=Chem.SanitizeFlags.SANITIZE_ALL ^ Chem.SanitizeFlags.SANITIZE_KEKULIZE
            )
        except Exception:
            pass
    out = copy_bond_dirs_from_product(
        target_mol, out, remove_idxs, anchor_orig_idxs, dummy_sites=dummy_sites
    )
    out = align_scaffold_2d_to_target(out, target_mol, remove_idxs, dummy_sites)
    if snaps:
        out = copy_core_chiral_tags_from_target(out, target_mol, snaps)
        fix_msgs = report_chiral_frame_side_mismatches(out, snaps)
        out = copy_bond_dirs_from_product(
            target_mol, out, remove_idxs, anchor_orig_idxs, dummy_sites=dummy_sites
        )
    else:
        fix_msgs = []
    return out, fix_msgs
