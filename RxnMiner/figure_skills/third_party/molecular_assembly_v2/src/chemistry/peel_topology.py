"""扣后结构检查：模板 mol vs 扣后展示 SMILES。

* → Ts (Z=117) 占位，同位素由 R 标签语义区分；比 InChIKey / canonical isomeric SMILES。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from rdkit import Chem

from esmiles import ESMILES_SEPARATOR
from chemistry.r_groups import normalize_r_tags, placeholder_isotope_from_r_label

# 占位：Tennessine (Z=117)；扣后骨架在 Sanitize 前即应使用，避免多 * 对称化抹手性
PLACEHOLDER_Z = 117
_PLACEHOLDER_Z = PLACEHOLDER_Z


def is_attachment_placeholder(atom: Chem.Atom) -> bool:
    """Dummy * (Z=0) or Ts placeholder (Z=117) on peeled scaffold / topology prep."""
    return atom.GetAtomicNum() in (0, PLACEHOLDER_Z)


def make_attachment_placeholder_atom(r_label: str = "") -> Chem.Atom:
    """Ts + semantic isotope from Synfacts connection label (before Sanitize)."""
    atom = Chem.Atom(PLACEHOLDER_Z)
    atom.SetIsotope(placeholder_isotope_from_r_label(r_label))
    return atom


@dataclass
class PeelTopologyResult:
    ok: bool
    note: str
    canonical_template: str = ""
    canonical_peeled: str = ""
    inchi_key_template: str = ""
    inchi_key_peeled: str = ""
    peel_display_smiles: str = ""
    nonisomeric_match: bool = False
    stereo_missing_hint: bool = False


@dataclass
class AttachmentLabelSymmetryResult:
    warn: bool
    symmetric_groups: list[list[str]]
    note: str = ""


@dataclass
class AttachmentDuplicateLabelResult:
    warn: bool
    duplicate_groups: list[dict[str, Any]]
    note: str = ""


@dataclass
class PeeledDuplicateLabelResult:
    warn: bool
    mismatch_groups: list[dict[str, Any]]
    note: str = ""


def check_template_duplicate_attachment_labels(tags: str) -> AttachmentDuplicateLabelResult:
    """Warn when the same connection label appears on multiple template ports."""
    slots = normalize_r_tags(tags or "")
    by_key: dict[str, list] = defaultdict(list)
    for slot in slots:
        by_key[slot.pair_key].append(slot)
    dups = {k: v for k, v in by_key.items() if len(v) > 1}
    if not dups:
        return AttachmentDuplicateLabelResult(False, [], "")

    duplicate_groups: list[dict[str, Any]] = []
    for label in sorted(dups):
        slot_list = dups[label]
        duplicate_groups.append(
            {
                "label": label,
                "positions": [s.smiles_position for s in slot_list],
                "slot_keys": [s.internal_id for s in slot_list],
            }
        )
    parts = [
        f"「{g['label']}」×{len(g['slot_keys'])}（{'、'.join(g['slot_keys'])}）"
        for g in duplicate_groups
    ]
    note = (
        f"产物 Markush 模板连接标签重复：{'；'.join(parts)}。"
        "文献图中同名无法区分；peel 时用 #2、#3 内部槽位跟踪。"
    )
    return AttachmentDuplicateLabelResult(True, duplicate_groups, note)


def _plain_substituent_smiles(raw: str) -> str:
    s = (raw or "").strip()
    if ESMILES_SEPARATOR in s:
        s = s.split(ESMILES_SEPARATOR, 1)[0].strip()
    return s


def _substituents_structurally_differ(smi_a: str, smi_b: str) -> bool:
    a = _plain_substituent_smiles(smi_a)
    b = _plain_substituent_smiles(smi_b)
    if not a and not b:
        return False
    if not a or not b:
        return True
    if a == b:
        return False
    from chemistry.chem_util import compare_mols_equivalence

    ma = Chem.MolFromSmiles(a)
    mb = Chem.MolFromSmiles(b)
    if ma is None or mb is None:
        return a != b
    ok, _ = compare_mols_equivalence(ma, mb)
    return not ok


def check_peeled_duplicate_label_substituents(
    anchors: list[dict[str, Any]],
    peeled: list[dict[str, Any]],
) -> PeeledDuplicateLabelResult:
    """Warn when duplicate template labels peel to chemically different groups."""
    id_to_smi: dict[str, str] = {}
    for item in peeled:
        smi = _plain_substituent_smiles(str(item.get("smiles") or ""))
        iid = str(item.get("internal_id") or "").strip()
        if iid and smi:
            id_to_smi[iid] = smi

    by_pair: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for anchor in anchors:
        pair_key = str(anchor.get("pair_key") or anchor.get("r_label") or "").strip()
        iid = str(anchor.get("internal_id") or "").strip()
        if not pair_key or not iid:
            continue
        smi = id_to_smi.get(iid, "")
        if smi:
            by_pair[pair_key].append((iid, smi))

    mismatch_groups: list[dict[str, Any]] = []
    for label, entries in sorted(by_pair.items()):
        unique_entries = list(dict.fromkeys(entries))
        if len(unique_entries) < 2:
            continue
        smis = [s for _, s in unique_entries]
        any_different = any(
            _substituents_structurally_differ(smis[i], smis[j])
            for i in range(len(smis))
            for j in range(i + 1, len(smis))
        )
        if not any_different:
            continue
        mismatch_groups.append(
            {
                "label": label,
                "slots": [{"slot_key": iid, "smiles": smi} for iid, smi in unique_entries],
            }
        )

    if not mismatch_groups:
        return PeeledDuplicateLabelResult(False, [], "")

    parts: list[str] = []
    for group in mismatch_groups:
        slot_bits = "、".join(
            f"{s['slot_key']}→{s['smiles'][:36]}{'…' if len(s['smiles']) > 36 else ''}"
            for s in group["slots"]
        )
        parts.append(f"「{group['label']}」({slot_bits})")
    note = (
        f"同名连接标签扣出不同取代基：{'；'.join(parts)}。"
        "模板标注无法区分同名片段，实际产物中应为不同结构。"
    )
    return PeeledDuplicateLabelResult(True, mismatch_groups, note)


def mol_for_attachment_symmetry_rank(mol: Chem.Mol) -> Chem.Mol | None:
    """Copy scaffold; clear Ts isotopes so ranks reflect graph symmetry only."""
    rw = Chem.RWMol(Chem.Mol(mol))
    for a in rw.GetAtoms():
        if is_attachment_placeholder(a):
            a.SetIsotope(0)
    try:
        Chem.SanitizeMol(rw)
    except Exception:
        return None
    return rw.GetMol()


def check_attachment_label_symmetry(
    mol: Chem.Mol,
    attachment_to_rlabel: dict[int, str],
) -> AttachmentLabelSymmetryResult:
    """Warn when distinct connection labels sit on symmetrically equivalent * sites."""
    if len(attachment_to_rlabel) < 2:
        return AttachmentLabelSymmetryResult(False, [], "")

    sym_mol = mol_for_attachment_symmetry_rank(mol)
    if sym_mol is None:
        return AttachmentLabelSymmetryResult(False, [], "")

    ranks = list(Chem.CanonicalRankAtoms(sym_mol, breakTies=False))
    rank_to_labels: dict[int, set[str]] = {}
    for atom_idx, label in attachment_to_rlabel.items():
        lbl = (label or "").strip()
        if not lbl or atom_idx < 0 or atom_idx >= sym_mol.GetNumAtoms():
            continue
        atom = sym_mol.GetAtomWithIdx(atom_idx)
        if not is_attachment_placeholder(atom):
            continue
        rank_to_labels.setdefault(ranks[atom_idx], set()).add(lbl)

    symmetric_groups: list[list[str]] = []
    for labels in rank_to_labels.values():
        if len(labels) < 2:
            continue
        group = sorted(labels)
        if group not in symmetric_groups:
            symmetric_groups.append(group)

    if not symmetric_groups:
        return AttachmentLabelSymmetryResult(False, [], "")

    parts = ["、".join(g) for g in symmetric_groups]
    note = (
        f"产物 Markush 模板上 {'；'.join(parts)} 处于对称等价位点；"
        "无额外立体/区域信息时，连接标签仅表示文献绘图顺序。"
    )
    return AttachmentLabelSymmetryResult(True, symmetric_groups, note)


def template_mol_from_smiles(
    smiles: str,
    dummy_to_rlabel: dict[int, str] | None = None,
) -> Chem.Mol | None:
    """Load template SMILES without sanitizing away ``@``, then *→Ts + semantic isotope."""
    mol = Chem.MolFromSmiles(smiles.strip(), sanitize=False)
    if mol is None:
        return None
    return mol_with_attachment_placeholders(mol, dummy_to_rlabel)


def mol_with_attachment_placeholders(
    mol: Chem.Mol,
    dummy_to_rlabel: dict[int, str] | None = None,
) -> Chem.Mol | None:
    """Ensure each attachment site is Ts (Z=117) with isotope from R label mapping."""
    dummy_to_rlabel = dummy_to_rlabel or {}
    rw = Chem.RWMol(Chem.Mol(mol))
    for a in rw.GetAtoms():
        if not is_attachment_placeholder(a):
            continue
        idx = a.GetIdx()
        if a.GetAtomicNum() == 0:
            a.SetAtomicNum(_PLACEHOLDER_Z)
        label = dummy_to_rlabel.get(idx, "")
        a.SetIsotope(placeholder_isotope_from_r_label(label))
    try:
        Chem.SanitizeMol(rw)
        Chem.AssignStereochemistry(rw, cleanIt=True, force=True)
    except Exception:
        return None
    return rw.GetMol()


def canonical_scaffold_smiles(
    mol: Chem.Mol,
    dummy_to_rlabel: dict[int, str] | None = None,
) -> str:
    """Canonical isomeric SMILES after * → Ts placeholders."""
    prepped = mol_with_attachment_placeholders(mol, dummy_to_rlabel)
    if prepped is None:
        return ""
    return Chem.MolToSmiles(prepped, canonical=True, isomericSmiles=True)


def topology_inchi_key(
    mol: Chem.Mol,
    dummy_to_rlabel: dict[int, str] | None = None,
) -> str:
    """Debug helper: InChIKey after same placeholder prep as comparison."""
    prepped = mol_with_attachment_placeholders(mol, dummy_to_rlabel)
    if prepped is None:
        return ""
    try:
        return Chem.MolToInchiKey(prepped)
    except Exception:
        return ""


def _nonisomeric_canonical_scaffold_smiles(
    mol: Chem.Mol,
    dummy_to_rlabel: dict[int, str] | None = None,
) -> str:
    prepped = mol_with_attachment_placeholders(mol, dummy_to_rlabel)
    if prepped is None:
        return ""
    return Chem.MolToSmiles(prepped, canonical=True, isomericSmiles=False)


def _has_double_bond_stereo(mol: Chem.Mol | None) -> bool:
    if mol is None:
        return False
    return any(
        bond.GetBondType() == Chem.BondType.DOUBLE
        and bond.GetStereo() != Chem.BondStereo.STEREONONE
        for bond in mol.GetBonds()
    )


def check_peeled_topology_matches_template(
    template_mol: Chem.Mol,
    peel_display_smiles: str,
    *,
    template_dummy_to_rlabel: dict[int, str] | None = None,
    peel_mol: Chem.Mol | None = None,
    peeled_dummy_to_rlabel: dict[int, str] | None = None,
) -> PeelTopologyResult:
    """模板 vs 扣后：Ts 占位 + 语义同位素 → canonical / InChIKey 一致则通过。"""
    smi = (peel_display_smiles or "").strip()
    template_dummy_to_rlabel = template_dummy_to_rlabel or {}
    peeled_dummy_to_rlabel = peeled_dummy_to_rlabel or {}

    if peel_mol is not None:
        peel_cmp = peel_mol
    else:
        if not smi:
            return PeelTopologyResult(False, "无扣后展示 SMILES", peel_display_smiles=smi)
        peel_cmp = Chem.MolFromSmiles(smi)
        if peel_cmp is None:
            return PeelTopologyResult(
                False,
                f"无法解析扣后 SMILES: {smi[:80]}",
                peel_display_smiles=smi,
            )

    try:
        can_t = canonical_scaffold_smiles(template_mol, template_dummy_to_rlabel)
        can_p = canonical_scaffold_smiles(peel_cmp, peeled_dummy_to_rlabel)
        plain_t = _nonisomeric_canonical_scaffold_smiles(template_mol, template_dummy_to_rlabel)
        plain_p = _nonisomeric_canonical_scaffold_smiles(peel_cmp, peeled_dummy_to_rlabel)
        key_t = topology_inchi_key(template_mol, template_dummy_to_rlabel) if can_t else ""
        key_p = topology_inchi_key(peel_cmp, peeled_dummy_to_rlabel) if can_p else ""
    except Exception as e:
        return PeelTopologyResult(
            False,
            f"规范化失败: {e}",
            peel_display_smiles=smi,
        )
    if not can_t or not can_p:
        return PeelTopologyResult(
            False,
            "canonical SMILES 为空（*→Ts 占位或 sanitize 失败）",
            canonical_template=can_t,
            canonical_peeled=can_p,
            inchi_key_template=key_t,
            inchi_key_peeled=key_p,
            peel_display_smiles=smi,
        )
    if key_t and key_p and key_t == key_p:
        return PeelTopologyResult(
            True,
            f"InChIKey 一致（{key_t}；Ts+语义同位素，拓扑+立体）",
            canonical_template=can_t,
            canonical_peeled=can_p,
            inchi_key_template=key_t,
            inchi_key_peeled=key_p,
            peel_display_smiles=smi,
        )
    if can_t == can_p:
        return PeelTopologyResult(
            True,
            "canonical isomeric SMILES 一致（Ts+语义同位素，拓扑+立体）",
            canonical_template=can_t,
            canonical_peeled=can_p,
            inchi_key_template=key_t,
            inchi_key_peeled=key_p,
            peel_display_smiles=smi,
        )
    noniso_match = bool(plain_t and plain_p and plain_t == plain_p)
    stereo_missing = (
        noniso_match
        and _has_double_bond_stereo(mol_with_attachment_placeholders(template_mol, template_dummy_to_rlabel))
        and not _has_double_bond_stereo(mol_with_attachment_placeholders(peel_cmp, peeled_dummy_to_rlabel))
    )
    short_t = can_t if len(can_t) <= 72 else can_t[:69] + "..."
    short_p = can_p if len(can_p) <= 72 else can_p[:69] + "..."
    note = f"canonical SMILES 不一致：模板 {short_t} ≠ 扣后 {short_p}"
    if stereo_missing:
        note += "；忽略双键立体后拓扑一致，疑似点选分子的 SMILES 未显式保存 E/Z 信息"
    return PeelTopologyResult(
        False,
        note,
        canonical_template=can_t,
        canonical_peeled=can_p,
        inchi_key_template=key_t,
        inchi_key_peeled=key_p,
        peel_display_smiles=smi,
        nonisomeric_match=noniso_match,
        stereo_missing_hint=stereo_missing,
    )
