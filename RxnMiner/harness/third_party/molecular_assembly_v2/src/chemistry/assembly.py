"""分子片段组装与 RDKit 绘图。

输入格式（每行一片段）::

    SMILES<sep><a>原子索引:R[标签]</a>...

- ``*`` 在 RDKit 中为 dummy（连接点）；标签必须标在 dummy 上。
- 相同 ``R[标签]`` 的连接点两两配对；片段间键级取自 dummy 与邻原子间的原有键级
  （单键 / 双键 / 三键，如 ``*C``、``C=*``、``C#*``）。
- 两侧键级不一致（例如一端 ``C=*``、另一端 ``*C``）时不连接，并写入 ``messages``
  供前端展示，产物中保留未连接 dummy（``partial=True``）。
- ``CombineMols`` 合并所有片段；模板与取代基在组装逻辑上无区别；第 0 行仅作
  ``scaffold_smiles`` 展示用。
- 片段 SMILES 中的 ``/`` ``\\`` 双键立体在合并后写回。
- 四面体接口：配对时把 dummy–core 上的楔/虚线（``BondDir``）迁到真实连接键上；
  手性碳上每发生一次 dummy→伙伴替换，在写回 ``ChiralTag`` 后按奇偶翻转 parity
  （RDKit tag 相对邻居序号；盲抄 ``@`` 会翻面）。不以 CIP R/S 为判据。
"""
from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass, field
from typing import Any

from rdkit import Chem
from rdkit.Chem import AllChem

from chemistry.peel_topology import is_attachment_placeholder

_CONNECTABLE_BOND_TYPES = frozenset(
    {Chem.BondType.SINGLE, Chem.BondType.DOUBLE, Chem.BondType.TRIPLE}
)

_BOND_TYPE_ZH: dict[Any, str] = {
    Chem.BondType.SINGLE: "单键",
    Chem.BondType.DOUBLE: "双键",
    Chem.BondType.TRIPLE: "三键",
    Chem.BondType.AROMATIC: "芳香键",
}

@dataclass
class AssemblyResult:
    success: bool
    partial: bool
    smiles: str
    scaffold_smiles: str
    messages: list[str] = field(default_factory=list)
    image_png_base64: str = ""


@dataclass
class DrawResult:
    ok: bool
    smiles: str = ""
    image_png_base64: str = ""
    error: str = ""


def parse_fragment_line(line: str) -> tuple[str, str] | None:
    from esmiles import parse_fragment_line as _split

    return _split(line)


def normalize_target_smiles(smiles: str) -> tuple[str, str | None]:
    """Normalize product input (concrete SMILES or fragment eSMILES).

    Delegates to :func:`esmiles.normalize_target_input` — see
    ``src/esmiles/README.md``.
    """
    from esmiles import normalize_target_input

    norm = normalize_target_input(smiles)
    return norm.smiles, norm.note


def diagnose_target_smiles_failure(smiles: str) -> str:
    """Human-readable reason when ``Chem.MolFromSmiles`` rejects a target."""
    s = (smiles or "").strip()
    if not s:
        return "产物 SMILES 为空。"
    if Chem.MolFromSmiles(s) is not None:
        return ""
    if "<sep>" in s:
        from esmiles import classify_target_esmiles, normalize_fragment_esmiles

        expanded = normalize_fragment_esmiles(s)
        if classify_target_esmiles(expanded) == "fragment":
            from esmiles import load_esmiles

            if load_esmiles(expanded, apply_dummy=True, prefer_indigo=False) is not None:
                return ""
            return "产物 fragment eSMILES（含开放连接位 *）无法加载。"
        body = expanded.split("<sep>", 1)[0].rstrip()
        suffix = expanded[expanded.find("<sep>") :]
        short = suffix if len(suffix) <= 56 else suffix[:53] + "…"
        if body and Chem.MolFromSmiles(body) is not None:
            return (
                "完整分子 SMILES 含 <sep> 及标签（应为具体结构，非 fragment 行）；"
                f"主体可解析，非法后缀：{short}"
            )
        return "完整分子 SMILES 含 <sep>，且主体也无法解析。"
    if "*" in s:
        return "完整分子含未展开连接位点 *（可能缺保护基 / 取代基展开）。"
    return "产物 SMILES 无法被 RDKit 解析（语法非法）。"


def scaffold_smiles_from_fragments(fragment_lines: list[str]) -> str | None:
    for line in fragment_lines:
        parsed = parse_fragment_line(line)
        if parsed:
            return parsed[0]
    return None


def _bond_type_label(bond_type: Chem.BondType) -> str:
    return _BOND_TYPE_ZH.get(bond_type, str(bond_type))


def _attachment_bond_type(rw_mol: Chem.RWMol, dummy_idx: int, real_idx: int) -> Chem.BondType:
    bond = rw_mol.GetBondBetweenAtoms(dummy_idx, real_idx)
    if bond is None:
        return Chem.BondType.SINGLE
    bt = bond.GetBondType()
    if bt in _CONNECTABLE_BOND_TYPES:
        return bt
    return Chem.BondType.SINGLE


def _connection_point(rw_mol: Chem.RWMol, atom: Chem.Atom) -> dict[str, Any] | None:
    """Connection metadata for a labeled attachment placeholder (* or Ts)."""
    if not is_attachment_placeholder(atom) or not atom.HasProp("_R_label"):
        return None
    dummy_idx = atom.GetIdx()
    neighbors = [n.GetIdx() for n in atom.GetNeighbors() if n.GetAtomicNum() > 0]
    if not neighbors:
        return None
    mn = atom.GetAtomMapNum()
    fragment_idx = (mn // 1000) - 1 if mn else 0
    return {
        "dummy_idx": dummy_idx,
        "real_idx": neighbors[0],
        "neighbor_idxs": neighbors,
        "degree": len(neighbors),
        "bond_type": _attachment_bond_type(rw_mol, dummy_idx, neighbors[0]),
        "fragment_idx": fragment_idx,
    }


def _connect_if_absent(
    rw_mol: Chem.RWMol,
    a_idx: int,
    b_idx: int,
    bond_type: Chem.BondType,
) -> bool:
    if a_idx == b_idx or rw_mol.GetBondBetweenAtoms(a_idx, b_idx) is not None:
        return False
    rw_mol.AddBond(a_idx, b_idx, bond_type)
    return True


def _invert_tetrahedral_chiral_tag(atom: Chem.Atom) -> None:
    tag = atom.GetChiralTag()
    if tag == Chem.ChiralType.CHI_TETRAHEDRAL_CW:
        atom.SetChiralTag(Chem.ChiralType.CHI_TETRAHEDRAL_CCW)
    elif tag == Chem.ChiralType.CHI_TETRAHEDRAL_CCW:
        atom.SetChiralTag(Chem.ChiralType.CHI_TETRAHEDRAL_CW)


def _record_chiral_neighbor_replacement(
    rw_mol: Chem.RWMol,
    core_idx: int,
    dummy_idx: int,
    partner_idx: int,
    replacement_records: dict[int, dict[str, Any]],
) -> None:
    """Record a semantic dummy→partner slot replacement for one chiral center.

    Do not infer parity from the number of replacements.  The only valid
    decision is the permutation from the old neighbor map sequence (with the
    dummy slot substituted by its partner map) to the actual post-delete
    neighbor sequence.
    """
    if core_idx < 0 or core_idx >= rw_mol.GetNumAtoms():
        return
    atom = rw_mol.GetAtomWithIdx(core_idx)
    if atom.GetChiralTag() == Chem.ChiralType.CHI_UNSPECIFIED:
        return
    center_map = int(atom.GetAtomMapNum() or 0)
    dummy_map = int(rw_mol.GetAtomWithIdx(dummy_idx).GetAtomMapNum() or 0)
    partner_map = int(rw_mol.GetAtomWithIdx(partner_idx).GetAtomMapNum() or 0)
    if not center_map or not dummy_map or not partner_map:
        return
    record = replacement_records.setdefault(
        center_map,
        {
            "old_neighbor_maps": [
                int(neighbor.GetAtomMapNum() or 0) for neighbor in atom.GetNeighbors()
            ],
            "slot_to_partner_map": {},
        },
    )
    record["slot_to_partner_map"][dummy_map] = partner_map


def _copy_dummy_attachment_bond_dir(
    rw_mol: Chem.RWMol,
    dummy_idx: int,
    core_idx: int,
    partner_idx: int,
) -> None:
    """Move ``/`` ``\\`` bond direction from a dummy–core bond onto partner–core."""
    old = rw_mol.GetBondBetweenAtoms(dummy_idx, core_idx)
    if old is None:
        return
    bond_dir = old.GetBondDir()
    if bond_dir == Chem.BondDir.NONE:
        return
    new = rw_mol.GetBondBetweenAtoms(core_idx, partner_idx)
    if new is None:
        return
    dummy_was_begin = old.GetBeginAtomIdx() == dummy_idx
    partner_is_begin = new.GetBeginAtomIdx() == partner_idx
    if dummy_was_begin == partner_is_begin:
        new.SetBondDir(bond_dir)
    else:
        new.SetBondDir(_flip_bond_dir(bond_dir))


def _connect_rgroup_pair(
    rw_mol: Chem.RWMol,
    a: dict[str, Any],
    b: dict[str, Any],
    bond_type: Chem.BondType,
    replacement_records: dict[int, dict[str, Any]] | None = None,
) -> bool:
    if replacement_records is not None:
        _record_chiral_neighbor_replacement(
            rw_mol, a["real_idx"], a["dummy_idx"], b["real_idx"], replacement_records
        )
        _record_chiral_neighbor_replacement(
            rw_mol, b["real_idx"], b["dummy_idx"], a["real_idx"], replacement_records
        )
    if not _connect_if_absent(rw_mol, a["real_idx"], b["real_idx"], bond_type):
        return False
    _copy_dummy_attachment_bond_dir(rw_mol, a["dummy_idx"], a["real_idx"], b["real_idx"])
    _copy_dummy_attachment_bond_dir(rw_mol, b["dummy_idx"], b["real_idx"], a["real_idx"])
    return True


def _replace_bridge_placeholders(
    rw_mol: Chem.RWMol,
    label: str,
    points: list[dict[str, Any]],
    say: Any,
    replacement_records: dict[int, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Replace a multivalent scaffold dummy with a matching two-ended bridge fragment."""
    remaining = list(points)
    atoms_to_remove: list[int] = []
    bridges = [p for p in remaining if p["degree"] > 1]

    for bridge in bridges:
        terminals = [p for p in remaining if p is not bridge and p["degree"] == 1]
        need = len(bridge["neighbor_idxs"])
        if need < 2:
            continue
        if len(terminals) < need:
            say(
                f"提示：{label} 桥占位需要 {need} 个片段端口，"
                f"但只有 {len(terminals)} 个。"
            )
            continue

        chosen = sorted(terminals, key=lambda p: p["dummy_idx"])[:need]
        core_neighbors = sorted(bridge["neighbor_idxs"])
        connected = 0
        for core_idx, term in zip(core_neighbors, chosen):
            if replacement_records is not None:
                _record_chiral_neighbor_replacement(
                    rw_mol,
                    core_idx,
                    bridge["dummy_idx"],
                    term["real_idx"],
                    replacement_records,
                )
                _record_chiral_neighbor_replacement(
                    rw_mol,
                    term["real_idx"],
                    term["dummy_idx"],
                    core_idx,
                    replacement_records,
                )
            if _connect_if_absent(rw_mol, core_idx, term["real_idx"], term["bond_type"]):
                _copy_dummy_attachment_bond_dir(
                    rw_mol,
                    term["dummy_idx"],
                    term["real_idx"],
                    core_idx,
                )
                connected += 1

        atoms_to_remove.append(bridge["dummy_idx"])
        atoms_to_remove.extend(p["dummy_idx"] for p in chosen)
        remove_ids = {id(bridge), *(id(p) for p in chosen)}
        remaining = [p for p in remaining if id(p) not in remove_ids]
        say(f"{label}：桥接替换 {connected}/{need} 个端口。")

    return remaining, atoms_to_remove


def _pair_scaffold_substituent_ports(
    rw_mol: Chem.RWMol,
    label: str,
    connection_points: list[dict[str, Any]],
    say: Any,
    replacement_records: dict[int, dict[str, Any]] | None = None,
) -> list[int]:
    """Connect fragment 0 (scaffold) ports only to later fragments (R-groups), never R↔R."""
    scaffold_pts = sorted(
        (p for p in connection_points if p.get("fragment_idx", 0) == 0),
        key=lambda p: p["dummy_idx"],
    )
    subst_pts = sorted(
        (p for p in connection_points if p.get("fragment_idx", 0) != 0),
        key=lambda p: p["dummy_idx"],
    )
    atoms_to_remove: list[int] = []
    paired = 0
    for s_pt, sub_pt in zip(scaffold_pts, subst_pts):
        bt_a = s_pt["bond_type"]
        bt_b = sub_pt["bond_type"]
        if bt_a != bt_b:
            say(
                f"警告：{label} 骨架与取代基键级不一致"
                f"（{_bond_type_label(bt_a)} vs {_bond_type_label(bt_b)}），未连接。"
            )
            continue
        if _connect_rgroup_pair(
            rw_mol, s_pt, sub_pt, bt_a, replacement_records
        ):
            atoms_to_remove.extend([s_pt["dummy_idx"], sub_pt["dummy_idx"]])
            paired += 1
        else:
            say(f"警告：{label} 骨架↔取代基无法连接（自键或已有键），已跳过。")
    if paired:
        say(f"{label}：已连接 {paired} 对（骨架↔取代基）。")
    extra_scaffold = len(scaffold_pts) - paired
    extra_subst = len(subst_pts) - paired
    if extra_scaffold:
        say(f"提示：{label} 骨架仍有 {extra_scaffold} 个未配对连接点。")
    if extra_subst:
        say(
            f"提示：{label} 有 {extra_subst} 个取代基端口未接到骨架"
            "（取代基之间不互连）。"
        )
    return atoms_to_remove


def _stamp_fragment_atom_maps(mols: list[Chem.Mol]) -> list[Chem.Mol]:
    """Tag each fragment atom with a unique AtomMapNum for stereo transfer after merge."""
    stamped: list[Chem.Mol] = []
    for fi, mol in enumerate(mols):
        base = (fi + 1) * 1000
        rw = Chem.RWMol(Chem.Mol(mol))
        for atom in rw.GetAtoms():
            atom.SetAtomMapNum(base + atom.GetIdx())
        stamped.append(rw.GetMol())
    return stamped


def _transfer_bond_stereo_from_fragments(
    assembled: Chem.RWMol,
    source_mols: list[Chem.Mol],
) -> None:
    """Copy cis/trans (``/`` ``\\``) stereo from pre-merge fragments onto ``assembled``."""
    map_to_idx: dict[int, int] = {}
    for atom in assembled.GetAtoms():
        mn = atom.GetAtomMapNum()
        if mn:
            map_to_idx[mn] = atom.GetIdx()

    for src in source_mols:
        for bond in src.GetBonds():
            stereo = bond.GetStereo()
            bond_dir = bond.GetBondDir()
            if stereo == Chem.BondStereo.STEREONONE and bond_dir == Chem.BondDir.NONE:
                continue
            begin = src.GetAtomWithIdx(bond.GetBeginAtomIdx()).GetAtomMapNum()
            end = src.GetAtomWithIdx(bond.GetEndAtomIdx()).GetAtomMapNum()
            ai = map_to_idx.get(begin)
            aj = map_to_idx.get(end)
            if ai is None or aj is None:
                continue
            out = assembled.GetBondBetweenAtoms(ai, aj)
            if out is None:
                continue
            if stereo != Chem.BondStereo.STEREONONE:
                out.SetStereo(stereo)
            if bond_dir != Chem.BondDir.NONE:
                if out.GetBeginAtomIdx() == ai:
                    out.SetBondDir(bond_dir)
                else:
                    out.SetBondDir(_flip_bond_dir(bond_dir))


def _clear_atom_map_nums(mol: Chem.RWMol) -> None:
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)


def _flip_bond_dir(bd: Chem.BondDir) -> Chem.BondDir:
    flip = {
        Chem.BondDir.BEGINWEDGE: Chem.BondDir.BEGINDASH,
        Chem.BondDir.BEGINDASH: Chem.BondDir.BEGINWEDGE,
        Chem.BondDir.ENDUPRIGHT: Chem.BondDir.ENDDOWNRIGHT,
        Chem.BondDir.ENDDOWNRIGHT: Chem.BondDir.ENDUPRIGHT,
    }
    return flip.get(bd, Chem.BondDir.NONE)


def _fragment_needs_stereo_safe_load(body: str) -> bool:
    """Markush lines with ``@`` beside ``*`` lose chirality under default ``load_esmiles``."""
    return "*" in body and ("@" in body or "@@" in body)


def _load_fragment_mol_for_assembly(frag_str: str, say: Any) -> Chem.Mol | None:
    """Load one tagged fragment; peel-aligned path keeps tetrahedral tags on Markush scaffolds."""
    from chemistry.anchors import anchors_from_esmiles, template_dummy_to_rlabel
    from esmiles import load_esmiles, parse_fragment_line
    from chemistry.peel_topology import template_mol_from_smiles

    frag_str = frag_str.strip()
    parsed = parse_fragment_line(frag_str)
    if parsed is None:
        return None
    body, _ = parsed

    loaded = load_esmiles(frag_str, prefer_indigo=False)
    if loaded is None:
        say(f"无法解析片段: {frag_str[:60]}…")
        return None

    mol = loaded.mol
    if _fragment_needs_stereo_safe_load(body):
        anchors = anchors_from_esmiles(mol, loaded)
        dummy_map = template_dummy_to_rlabel(anchors)
        tmpl = template_mol_from_smiles(body, dummy_map)
        if tmpl is not None:
            mol = tmpl
            for atom in mol.GetAtoms():
                if not is_attachment_placeholder(atom):
                    continue
                label = dummy_map.get(atom.GetIdx())
                if label:
                    atom.SetProp("_R_label", label)
        else:
            say(f"警告：立体 Markush 片段未能以 template_mol 加载: {body[:48]}…")

    if not any(atom.HasProp("_R_label") for atom in mol.GetAtoms()):
        for slot in loaded.anchor_slots:
            if slot.dummy_idx < 0 or slot.dummy_idx >= mol.GetNumAtoms():
                say(
                    f"警告：标签 {slot.source_label} 位置 {slot.smiles_position} "
                    f"索引无效，已跳过。"
                )
                continue
            atom = mol.GetAtomWithIdx(slot.dummy_idx)
            if is_attachment_placeholder(atom):
                atom.SetProp("_R_label", slot.source_label)
            else:
                say(
                    f"警告：标签 {slot.source_label} 位置 {slot.smiles_position} "
                    f"不是虚拟原子，已跳过。"
                )
    return mol


def _transfer_chiral_tags_from_fragments(
    assembled: Chem.RWMol,
    source_mols: list[Chem.Mol],
) -> None:
    """Copy tetrahedral tags from pre-merge fragments via stamped AtomMapNum."""
    map_to_idx: dict[int, int] = {}
    for atom in assembled.GetAtoms():
        mn = atom.GetAtomMapNum()
        if mn:
            map_to_idx[mn] = atom.GetIdx()

    for src in source_mols:
        for atom in src.GetAtoms():
            tag = atom.GetChiralTag()
            if tag == Chem.ChiralType.CHI_UNSPECIFIED:
                continue
            mn = atom.GetAtomMapNum()
            ai = map_to_idx.get(mn)
            if ai is not None:
                assembled.GetAtomWithIdx(ai).SetChiralTag(tag)


def _apply_chiral_parity_after_dummy_replacements(
    assembled: Chem.RWMol,
    replacement_records: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply only the real neighbor-sequence permutation after dummy deletion."""
    if not replacement_records:
        return []
    map_to_idx: dict[int, int] = {}
    for atom in assembled.GetAtoms():
        mn = atom.GetAtomMapNum()
        if mn:
            map_to_idx[int(mn)] = atom.GetIdx()

    decisions: list[dict[str, Any]] = []
    for center_map, record in replacement_records.items():
        ai = map_to_idx.get(int(center_map))
        old_neighbors = [int(m) for m in record.get("old_neighbor_maps", []) if m]
        replacements = {
            int(old): int(new)
            for old, new in (record.get("slot_to_partner_map") or {}).items()
        }
        expected = [replacements.get(atom_map, atom_map) for atom_map in old_neighbors]
        if ai is None:
            decisions.append(
                {
                    "center_map": center_map,
                    "expected_neighbor_maps": expected,
                    "actual_neighbor_maps": [],
                    "flip": False,
                    "reason": "center removed / absent",
                }
            )
            continue
        actual = [
            int(neighbor.GetAtomMapNum() or 0)
            for neighbor in assembled.GetAtomWithIdx(ai).GetNeighbors()
        ]
        if (
            not expected
            or len(expected) != len(actual)
            or len(set(expected)) != len(expected)
            or set(expected) != set(actual)
        ):
            decisions.append(
                {
                    "center_map": center_map,
                    "expected_neighbor_maps": expected,
                    "actual_neighbor_maps": actual,
                    "flip": False,
                    "reason": "neighbor identities differ; no unsafe automatic flip",
                }
            )
            continue
        order = [expected.index(atom_map) for atom_map in actual]
        inversions = sum(
            1
            for i, value in enumerate(order)
            for later in order[i + 1 :]
            if value > later
        )
        flip = bool(inversions % 2)
        if flip:
            _invert_tetrahedral_chiral_tag(assembled.GetAtomWithIdx(ai))
        decisions.append(
            {
                "center_map": center_map,
                "expected_neighbor_maps": expected,
                "actual_neighbor_maps": actual,
                "permutation": order,
                "inversions": inversions,
                "flip": flip,
                "reason": "odd permutation" if flip else "even permutation",
            }
        )
    return decisions


def _finalize_assembled_stereo(
    mol: Chem.Mol,
    source_mols: list[Chem.Mol],
    replacement_records: dict[int, dict[str, Any]] | None = None,
    trace: list[dict[str, Any]] | None = None,
    tracked_atom_maps: set[int] | None = None,
) -> Chem.Mol:
    def atom_details(snapshot_mol: Chem.Mol) -> list[dict[str, Any]]:
        maps = tracked_atom_maps or set()
        by_map = {
            int(atom.GetAtomMapNum()): atom
            for atom in snapshot_mol.GetAtoms()
            if atom.GetAtomMapNum()
        }
        out: list[dict[str, Any]] = []
        for atom_map in sorted(maps):
            atom = by_map.get(atom_map)
            if atom is None:
                out.append({"atom_map": atom_map, "state": "removed / absent"})
                continue
            out.append(
                {
                    "atom_map": atom_map,
                    "state": "present",
                    "idx": atom.GetIdx(),
                    "symbol": atom.GetSymbol(),
                    "isotope": atom.GetIsotope(),
                    "chiral_tag": str(atom.GetChiralTag()),
                    "neighbors": [
                        {
                            "atom_map": neighbor.GetAtomMapNum(),
                            "symbol": neighbor.GetSymbol(),
                            "idx": neighbor.GetIdx(),
                        }
                        for neighbor in atom.GetNeighbors()
                    ],
                }
            )
        return out

    def snapshot(stage: str, snapshot_mol: Chem.Mol) -> None:
        if trace is not None:
            trace.append(
                {
                    "stage": stage,
                    "mol": Chem.Mol(snapshot_mol),
                    "details": {
                        "replacement_records": replacement_records or {},
                        "parity_decisions": list(parity_decisions),
                        "tracked_atom_maps": sorted(tracked_atom_maps or set()),
                        "tracked_atoms": atom_details(snapshot_mol),
                    },
                }
            )

    parity_decisions: list[dict[str, Any]] = []
    rw = Chem.RWMol(Chem.Mol(mol))
    # Maps must stay until after chiral invert (bond-stereo used to clear them early).
    _transfer_bond_stereo_from_fragments(rw, source_mols)
    snapshot("finalize: bond stereo copied from source fragments", rw.GetMol())
    _transfer_chiral_tags_from_fragments(rw, source_mols)
    snapshot("finalize: ChiralTag copied from source fragments", rw.GetMol())
    parity_decisions = _apply_chiral_parity_after_dummy_replacements(
        rw,
        replacement_records or {},
    )
    snapshot("finalize: actual neighbor permutation parity applied", rw.GetMol())
    _clear_atom_map_nums(rw)
    snapshot("finalize: AtomMapNum cleared", rw.GetMol())
    out = rw.GetMol()
    try:
        Chem.AssignStereochemistry(out, cleanIt=False, force=False)
    except Exception:
        pass
    snapshot("finalize: AssignStereochemistry complete", out)
    return out


def build_complete_molecule(
    fragment_strings: list[str],
    messages: list[str] | None = None,
    trace: list[dict[str, Any]] | None = None,
) -> Chem.Mol | None:
    """Assemble tagged fragments; connect pairs per ``<a>`` label.

    ``trace`` is an optional diagnostic sink.  Its copied Mol snapshots expose
    the real assembly states without opening another graft implementation.
    """
    log = messages if messages is not None else []

    def say(msg: str) -> None:
        log.append(msg)

    def snapshot(
        stage: str,
        mol: Chem.Mol | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if trace is not None and mol is not None:
            trace.append(
                {
                    "stage": stage,
                    "mol": Chem.Mol(mol),
                    "details": details or {},
                }
            )

    def trace_atom_details(
        mol: Chem.Mol,
        atom_maps: set[int],
    ) -> list[dict[str, Any]]:
        """Describe the same mapped atoms before and after dummy deletion."""
        by_map = {
            int(atom.GetAtomMapNum()): atom
            for atom in mol.GetAtoms()
            if atom.GetAtomMapNum()
        }
        details: list[dict[str, Any]] = []
        for atom_map in sorted(atom_maps):
            atom = by_map.get(atom_map)
            if atom is None:
                details.append({"atom_map": atom_map, "state": "removed / absent"})
                continue
            # Snapshot 4 intentionally precedes sanitization / dummy deletion.
            # Never ask RDKit for implicit H before its property cache exists:
            # that logs a pre-condition violation even if caught.
            total_hydrogens = (
                atom.GetTotalNumHs() if not mol.NeedsUpdatePropertyCache() else None
            )
            details.append(
                {
                    "atom_map": atom_map,
                    "state": "present",
                    "idx": atom.GetIdx(),
                    "symbol": atom.GetSymbol(),
                    "atomic_num": atom.GetAtomicNum(),
                    "isotope": atom.GetIsotope(),
                    "total_hydrogens": total_hydrogens,
                    "chiral_tag": str(atom.GetChiralTag()),
                    "neighbors": [
                        {
                            "atom_map": neighbor.GetAtomMapNum(),
                            "symbol": neighbor.GetSymbol(),
                            "idx": neighbor.GetIdx(),
                        }
                        for neighbor in atom.GetNeighbors()
                    ],
                }
            )
        return details

    mols: list[Chem.Mol] = []

    for frag_str in fragment_strings:
        frag_str = frag_str.strip()
        if not frag_str or "<sep>" not in frag_str:
            continue
        mol = _load_fragment_mol_for_assembly(frag_str, say)
        if mol is not None:
            mols.append(mol)

    if not mols:
        return None

    snapshot("assembly: fragments loaded (Ts / semantic isotopes retained)", mols[0])
    stamped_mols = _stamp_fragment_atom_maps(mols)
    combined_mol = stamped_mols[0]
    for m in stamped_mols[1:]:
        combined_mol = Chem.CombineMols(combined_mol, m)

    rw_mol = Chem.RWMol(combined_mol)
    snapshot("assembly: fragments merged (before connection)", rw_mol.GetMol())
    r_group_map: dict[str, list[dict[str, Any]]] = {}
    for atom in rw_mol.GetAtoms():
        point = _connection_point(rw_mol, atom)
        if point is None:
            continue
        label = atom.GetProp("_R_label")
        r_group_map.setdefault(label, []).append(point)

    replacement_records: dict[int, dict[str, Any]] = {}
    atoms_to_remove: list[int] = []
    removal_labels: dict[int, str] = {}
    for label, connection_points in list(r_group_map.items()):
        connection_points, removed = _replace_bridge_placeholders(
            rw_mol, label, list(connection_points), say, replacement_records
        )
        atoms_to_remove.extend(removed)
        removal_labels.update({idx: label for idx in removed})

        paired_removed = _pair_scaffold_substituent_ports(
            rw_mol, label, list(connection_points), say, replacement_records
        )
        atoms_to_remove.extend(paired_removed)
        removal_labels.update({idx: label for idx in paired_removed})

    pending_removals: list[dict[str, Any]] = []
    tracked_atom_maps: set[int] = set()
    for idx in sorted(set(atoms_to_remove)):
        if idx < 0 or idx >= rw_mol.GetNumAtoms():
            continue
        atom = rw_mol.GetAtomWithIdx(idx)
        if atom.GetAtomMapNum():
            tracked_atom_maps.add(int(atom.GetAtomMapNum()))
        for neighbor in atom.GetNeighbors():
            if neighbor.GetAtomMapNum():
                tracked_atom_maps.add(int(neighbor.GetAtomMapNum()))
            # A hand-drawn stereo frame is read around the chiral center, so
            # retain its other one-bond neighbors as well (CH2 / carbonyl etc.).
            for center_neighbor in neighbor.GetNeighbors():
                if center_neighbor.GetAtomMapNum():
                    tracked_atom_maps.add(int(center_neighbor.GetAtomMapNum()))
        pending_removals.append(
            {
                "label": removal_labels.get(idx, ""),
                "idx": idx,
                "atom_map": atom.GetAtomMapNum(),
                "atomic_num": atom.GetAtomicNum(),
                "isotope": atom.GetIsotope(),
                "neighbors": [
                    {
                        "idx": neighbor.GetIdx(),
                        "atom_map": neighbor.GetAtomMapNum(),
                        "atomic_num": neighbor.GetAtomicNum(),
                        "chiral_tag": str(neighbor.GetChiralTag()),
                    }
                    for neighbor in atom.GetNeighbors()
                ],
            }
        )
    cross_fragment_bonds = [
        {
            "begin_map": bond.GetBeginAtom().GetAtomMapNum(),
            "end_map": bond.GetEndAtom().GetAtomMapNum(),
            "bond_type": str(bond.GetBondType()),
            "bond_dir": str(bond.GetBondDir()),
        }
        for bond in rw_mol.GetBonds()
        if (
            bond.GetBeginAtom().GetAtomMapNum()
            and bond.GetEndAtom().GetAtomMapNum()
            and (bond.GetBeginAtom().GetAtomMapNum() // 1000)
            != (bond.GetEndAtom().GetAtomMapNum() // 1000)
        )
    ]
    snapshot(
        "assembly: partners connected (dummies not removed)",
        rw_mol.GetMol(),
        {
            "pending_removals": pending_removals,
            "cross_fragment_bonds": cross_fragment_bonds,
            "replacement_records": replacement_records,
            "tracked_atom_maps": sorted(tracked_atom_maps),
            "tracked_atoms": trace_atom_details(rw_mol.GetMol(), tracked_atom_maps),
        },
    )
    for idx in sorted(set(atoms_to_remove), reverse=True):
        rw_mol.RemoveAtom(idx)

    final_mol = rw_mol.GetMol()
    snapshot(
        "assembly: dummies removed (before stereo finalize)",
        final_mol,
        {
            "tracked_atom_maps": sorted(tracked_atom_maps),
            "tracked_atoms": trace_atom_details(final_mol, tracked_atom_maps),
            "removed_dummy_atom_maps": sorted(
                item["atom_map"] for item in pending_removals if item["atom_map"]
            ),
        },
    )
    final_mol = _finalize_assembled_stereo(
        final_mol,
        stamped_mols,
        replacement_records,
        trace=trace,
        tracked_atom_maps=tracked_atom_maps,
    )
    try:
        Chem.SanitizeMol(final_mol)
    except Exception as e:
        say(f"分子 Sanitize 失败: {e}")

    snapshot("assembly: stereo finalized (Ts placeholders retained)", final_mol)
    return final_mol


def has_unconnected_dummy(mol: Chem.Mol) -> bool:
    return any(is_attachment_placeholder(atom) for atom in mol.GetAtoms())


def mol_to_png_base64(mol: Chem.Mol, size: tuple[int, int] = (400, 300)) -> str:
    from rdkit.Chem import Draw

    img = Draw.MolToImage(mol, size=size)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def mol_to_png_with_atom_notes_base64(
    mol: Chem.Mol,
    *,
    size: tuple[int, int] = (400, 300),
    atom_notes: dict[int, str] | None = None,
    stereo_annotation: bool = False,
    annotation_font_scale: float = 0.85,
) -> str:
    """PNG with ``atomNote`` beside atoms (keeps ``*``); labels from ``<a>`` tags."""
    from rdkit.Chem.Draw import rdMolDraw2D

    rw = Chem.Mol(mol)
    notes = atom_notes or {}
    for idx, text in notes.items():
        if 0 <= idx < rw.GetNumAtoms() and text:
            rw.GetAtomWithIdx(idx).SetProp("atomNote", str(text))

    w, h = size
    drawer = rdMolDraw2D.MolDraw2DCairo(w, h)
    opts = drawer.drawOptions()
    opts.addAtomIndices = False
    opts.addStereoAnnotation = stereo_annotation
    opts.annotationFontScale = annotation_font_scale
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, rw)
    drawer.FinishDrawing()
    return base64.b64encode(drawer.GetDrawingText()).decode("ascii")


def short_r_display_label(r_label: str) -> str:
    """R[4]' -> R4' for on-structure text (see r_groups.display_label_for_occurrence)."""
    from chemistry.r_groups import short_display_from_source

    return short_display_from_source(r_label)


def _restore_bond_dirs_from_source(dst: Chem.RWMol, src: Chem.Mol) -> None:
    """Re-apply wedge/dash after Compute2DCoords (same topology, same atom indices)."""
    for b in src.GetBonds():
        bd = b.GetBondDir()
        if bd == Chem.BondDir.NONE:
            continue
        i, j = b.GetBeginAtomIdx(), b.GetEndAtomIdx()
        out = dst.GetBondBetweenAtoms(i, j)
        if out is not None:
            out.SetBondDir(bd)


def _restore_chiral_tags_from_source(dst: Chem.RWMol, src: Chem.Mol) -> None:
    for a in src.GetAtoms():
        tag = a.GetChiralTag()
        if tag != Chem.ChiralType.CHI_UNSPECIFIED:
            dst.GetAtomWithIdx(a.GetIdx()).SetChiralTag(tag)


def _is_stereogenic_center(atom: Chem.Atom) -> bool:
    return atom.GetChiralTag() not in (
        Chem.ChiralType.CHI_UNSPECIFIED,
        Chem.ChiralType.CHI_OTHER,
    )


def _sanitize_depiction_bond_dirs(mol: Chem.Mol) -> Chem.Mol:
    """Drop wedge/hash bonds that do not start at a stereogenic center.

    Product peel may copy BondDir onto heteroatom bonds (e.g. thioester S–C);
    RDKit then omits the bond line in 2D depictions.
    """
    rw = Chem.RWMol(Chem.Mol(mol))
    for b in rw.GetBonds():
        if b.GetBondDir() == Chem.BondDir.NONE:
            continue
        begin = rw.GetAtomWithIdx(b.GetBeginAtomIdx())
        if not _is_stereogenic_center(begin):
            b.SetBondDir(Chem.BondDir.NONE)
    return rw.GetMol()


def peel_scaffold_depiction_mol(
    mol: Chem.Mol,
    atom_labels: dict[int, str] | None = None,
) -> Chem.Mol:
    """Same 2D layout + stereo props as ``mol_to_png_peel_scaffold_base64`` (before PNG)."""
    src = Chem.Mol(mol)
    rw = Chem.RWMol(Chem.Mol(src))
    labels = atom_labels or {}
    for idx, text in labels.items():
        if 0 <= idx < rw.GetNumAtoms():
            rw.GetAtomWithIdx(idx).SetProp("atomLabel", text)
    if rw.GetNumConformers() == 0:
        AllChem.Compute2DCoords(rw)
    else:
        conf = rw.GetConformer()
        coord_map = {i: conf.GetAtomPosition(i) for i in range(rw.GetNumAtoms())}
        try:
            AllChem.Compute2DCoords(rw, coordMap=coord_map, canonOrient=False)
        except TypeError:
            pass
    _restore_chiral_tags_from_source(rw, src)
    _restore_bond_dirs_from_source(rw, src)
    return _sanitize_depiction_bond_dirs(rw.GetMol())


def peel_scaffold_mol_for_topology_check(
    mol: Chem.Mol,
    *,
    template_mol: Chem.Mol | None = None,
    template_to_full: dict[int, int] | None = None,
) -> Chem.Mol:
    """Same mol pipeline as ``peel_scaffold_smiles_for_display`` (before SMILES round-trip)."""
    dep = peel_scaffold_depiction_mol(mol)
    if template_mol is not None and template_to_full:
        full_to_t = {int(v): int(k) for k, v in template_to_full.items()}
        rw = Chem.RWMol(Chem.Mol(dep))
        for a in rw.GetAtoms():
            if not a.HasProp("_origIdx"):
                continue
            orig = int(a.GetProp("_origIdx"))
            t_idx = full_to_t.get(orig)
            if t_idx is None:
                continue
            tag_t = template_mol.GetAtomWithIdx(t_idx).GetChiralTag()
            if tag_t != Chem.ChiralType.CHI_UNSPECIFIED:
                rw.GetAtomWithIdx(a.GetIdx()).SetChiralTag(tag_t)
        dep = rw.GetMol()
    return dep


def peel_scaffold_smiles_for_display(
    mol: Chem.Mol,
    *,
    template_mol: Chem.Mol | None = None,
    template_to_full: dict[int, int] | None = None,
) -> str:
    """Isomeric SMILES from the depiction mol (same pipeline as peel PNG).

    Wedge/dash live in BondDir and are not fully round-tripped by SMILES alone.
    For mapped chiral centers, use the template ``ChiralTag`` so the string matches
    the scaffold-line ``@``/``@@`` convention (e.g. ``[C@]`` not a misleading ``[C@@]``).
    """
    dep = peel_scaffold_mol_for_topology_check(
        mol, template_mol=template_mol, template_to_full=template_to_full
    )
    return Chem.MolToSmiles(dep, isomericSmiles=True)


def mol_to_png_peel_scaffold_base64(
    mol: Chem.Mol,
    *,
    size: tuple[int, int] = (520, 360),
    atom_labels: dict[int, str] | None = None,
    atom_notes: dict[int, str] | None = None,
) -> str:
    """Peeled scaffold PNG: default 2D spread + bond dirs/tags from peeled mol."""
    dep = peel_scaffold_depiction_mol(mol, atom_labels if atom_notes is None else None)
    if atom_notes:
        rw = Chem.RWMol(Chem.Mol(dep))
        for idx, text in atom_notes.items():
            if 0 <= idx < rw.GetNumAtoms() and text:
                rw.GetAtomWithIdx(idx).SetProp("atomNote", str(text))
        dep = rw.GetMol()
    rw = Chem.RWMol(dep)
    from rdkit.Chem import Draw

    img = Draw.MolToImage(rw, size=size, fitImage=True, kekulize=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def mol_to_png_labeled_base64(
    mol: Chem.Mol,
    *,
    size: tuple[int, int] = (520, 360),
    atom_labels: dict[int, str] | None = None,
    highlight_atoms: list[int] | None = None,
    highlight_bonds: list[int] | None = None,
    atom_colors: dict[int, tuple[float, float, float]] | None = None,
    stereo_annotation: bool = True,
    annotation_font_scale: float = 1.0,
    min_font_size: float = 6,
    max_font_size: float = 40,
) -> str:
    """PNG with atomLabel text (e.g. R4') via RDKit MolDraw2D."""
    from rdkit.Chem.Draw import rdMolDraw2D

    rw = Chem.Mol(mol)
    labels = atom_labels or {}
    for idx, text in labels.items():
        if 0 <= idx < rw.GetNumAtoms():
            rw.GetAtomWithIdx(idx).SetProp("atomLabel", text)

    # Keep user-facing Markush cards on the same depiction protocol as the
    # assembly test viewer: coordinates are generated explicitly before
    # PrepareAndDrawMolecule.  Letting the drawer generate them implicitly can
    # choose a different local order around a tetrahedral centre even for the
    # same eSMILES body.
    if rw.GetNumConformers() == 0:
        AllChem.Compute2DCoords(rw)

    w, h = size
    drawer = rdMolDraw2D.MolDraw2DCairo(w, h)
    opts = drawer.drawOptions()
    opts.addAtomIndices = False
    opts.addStereoAnnotation = stereo_annotation
    opts.legendFontSize = 14
    opts.annotationFontScale = annotation_font_scale
    opts.minFontSize = min_font_size
    opts.maxFontSize = max_font_size

    ha = list(highlight_atoms or [])
    hb = list(highlight_bonds or [])
    hac: dict[int, tuple[float, float, float]] = {}
    if atom_colors:
        for idx, rgb in atom_colors.items():
            if idx in ha or labels.get(idx):
                hac[idx] = rgb

    rdMolDraw2D.PrepareAndDrawMolecule(
        drawer,
        rw,
        highlightAtoms=ha,
        highlightBonds=hb,
        highlightAtomColors=hac if hac else None,
    )
    drawer.FinishDrawing()
    png = drawer.GetDrawingText()
    return base64.b64encode(png).decode("ascii")


def draw_single_fragment(fragment_str: str) -> DrawResult:
    fragment_str = fragment_str.strip()
    if not fragment_str:
        return DrawResult(ok=False, error="输入为空。")
    parsed = parse_fragment_line(fragment_str)
    if not parsed:
        return DrawResult(ok=False, error="格式需包含 <sep>，例如 *C1=CC=CC=C1<sep><a>0:R[2]</a>")
    smiles, _ = parsed
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return DrawResult(ok=False, error=f"无法解析 SMILES: {smiles}")
    try:
        Chem.SanitizeMol(mol)
    except Exception:
        pass
    return DrawResult(
        ok=True,
        smiles=Chem.MolToSmiles(mol),
        image_png_base64=_card_png(mol),
    )


def _card_png(mol: Chem.Mol, *, size: tuple[int, int] = (520, 360)) -> str:
    """Draw an assembly result with the shared card depiction protocol."""
    from chemistry.rendering import render_display_mol

    return render_display_mol(mol, size=size)


def assemble_fragments(
    fragment_lines: list[str],
) -> AssemblyResult:
    lines = [ln.strip() for ln in fragment_lines if ln and ln.strip()]
    messages: list[str] = []

    scaffold_smi = scaffold_smiles_from_fragments(lines) or ""
    scaffold_mol = Chem.MolFromSmiles(scaffold_smi) if scaffold_smi else None
    if scaffold_mol is None and scaffold_smi:
        messages.append(f"无法解析核心骨架 SMILES: {scaffold_smi}")

    assembled = build_complete_molecule(
        lines,
        messages=messages,
    )

    if assembled is None:
        display_mol = scaffold_mol
        display_smiles = scaffold_smi
        if display_mol is None:
            return AssemblyResult(
                success=False,
                partial=False,
                smiles="",
                scaffold_smiles=scaffold_smi,
                messages=messages or ["无有效片段输入。"],
            )
        messages.insert(0, "组装失败，仅展示核心骨架。")
        return AssemblyResult(
            success=False,
            partial=False,
            smiles=display_smiles,
            scaffold_smiles=scaffold_smi,
            messages=messages,
            image_png_base64=_card_png(display_mol),
        )

    display_mol = assembled
    display_smiles = Chem.MolToSmiles(assembled, isomericSmiles=True)
    success = not has_unconnected_dummy(assembled)
    partial = not success

    if success:
        messages.insert(0, "组装成功：所有连接位点均已配对。")
    else:
        messages.insert(0, "部分组装：展示当前已拼接结果（仍有未连接位点）。")

    return AssemblyResult(
        success=success,
        partial=partial,
        smiles=display_smiles,
        scaffold_smiles=scaffold_smi,
        messages=messages,
        image_png_base64=_card_png(display_mol, size=(520, 400)),
    )
