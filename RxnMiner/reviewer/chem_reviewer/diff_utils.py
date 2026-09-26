"""Compute structured + human-readable diffs between two reviewer JSON docs.

We don't use jsonpatch / deepdiff (external deps) — write our own walker tuned
to the chem_reviewer schema. The output is two artifacts:

1. JSON diff (`compute_json_diff`)
   List of operation dicts, each:
       {"op": "add"|"remove"|"replace", "path": "/reaction_templates/0/reactants/1/E-SMILES",
        "old": <value or omitted>, "new": <value or omitted>}
   Path syntax = JSON Pointer (RFC 6901).

2. Markdown diff (`compute_markdown_diff`)
   Chemist-friendly bullet list with field labels in plain Chinese, grouped by
   reaction template / example. Example:

       ## T3 · Scheme 4
       - 修改 step3-21to22 的 yield: `76%` → `78%`
       - 增加 reactant: `2,2,2-trichloro-...` (identifier=24)
       - 删除 condition (step 1 catalyst): `"Pd(PPh3)4"`
"""
from __future__ import annotations

import json
from typing import Any, Iterable

# ── JSON-pointer helpers ───────────────────────────────────────────────


def _escape_token(t: str) -> str:
    return str(t).replace("~", "~0").replace("/", "~1")


def _join_pointer(parts: Iterable[Any]) -> str:
    return "/" + "/".join(_escape_token(p) for p in parts) if parts else ""


# ── Structural diff (RFC 6902-ish) ─────────────────────────────────────


def _diff(old: Any, new: Any, path: list[Any], ops: list[dict]) -> None:
    if old == new:
        return

    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            if k in old and k not in new:
                ops.append({"op": "remove", "path": _join_pointer(path + [k]), "old": old[k]})
            elif k not in old and k in new:
                ops.append({"op": "add", "path": _join_pointer(path + [k]), "new": new[k]})
            else:
                _diff(old[k], new[k], path + [k], ops)
        return

    if isinstance(old, list) and isinstance(new, list):
        # Naive but readable: compare by index.  If lengths differ we emit
        # add/remove for the tail; mismatched elements within the common prefix
        # become recursive diffs.  This works well for our schema where lists
        # are reactants/products/conditions/examples (small, ordered, mostly
        # identity-stable across edits).
        common = min(len(old), len(new))
        for i in range(common):
            _diff(old[i], new[i], path + [i], ops)
        if len(new) > common:
            for i in range(common, len(new)):
                ops.append({"op": "add", "path": _join_pointer(path + [i]), "new": new[i]})
        if len(old) > common:
            for i in range(common, len(old)):
                ops.append({"op": "remove", "path": _join_pointer(path + [i]), "old": old[i]})
        return

    # Scalar or type mismatch
    ops.append({"op": "replace", "path": _join_pointer(path) or "/", "old": old, "new": new})


def compute_json_diff(original: Any, edited: Any) -> list[dict]:
    """Return a list of structured diff operations (RFC 6902-style)."""
    ops: list[dict] = []
    _diff(original, edited, [], ops)
    return ops


# ── Human-readable Markdown ─────────────────────────────────────────────


def _short_value(v: Any, limit: int = 80) -> str:
    if v is None:
        return "_(空)_"
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    s = s.replace("\n", " ")
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return f"`{s}`"


# Path → human label.  Best-effort heuristic.
_FIELD_LABELS_CN = {
    "reaction_templates": "模板",
    "selected_examples": "实施例",
    "reactants": "反应物",
    "products": "产物",
    "conditions": "条件",
    "metrics": "指标",
    "texts": "文本",
    "identifier": "标识符",
    "E-SMILES": "结构 (SMILES)",
    "description": "描述",
    "role": "角色",
    "note": "备注",
    "text": "内容",
    "_meta": "元信息",
    "source_caption": "来源 caption",
    "source_subpanel": "子图编号",
    "source_page": "页码",
    "_dropped": "标记非反应",
    "_dropped_reason": "非反应理由",
    "instance_id": "实例 ID",
    "inherit_template": "继承模板",
    "markush_enumeration": "Markush 代入",
    "template_id": "模板 ID",
    # ── v2 schema additions ───────────────────────────────────
    "task_id": "任务 ID",
    "paper_id": "论文 ID",
    "anchor_image": "anchor 图",
    "anchor_figure": "anchor 标题",
    "anchor_page": "anchor 页码",
    "anchor_bbox_pdf": "anchor PDF bbox",
    "molparser_detection_count": "MolParser 检测数",
    "extractor": "抽取器",
    "identifier_map": "ID 映射",
    "is_multistep_synthesis": "是否多步合成",
    "synthesis_overview": "合成总览",
    "name": "名称",
    "type": "类型",
    "n_templates": "模板数",
    "n_examples": "实施例数",
    "starting_material_identifier": "起始物 ID",
    "final_product_identifier": "终产物 ID",
    "step_sequence": "步骤序列",
    "reaction_graph_edge_count": "DAG 边数",
    "reaction_graph": "反应 DAG",
    "edge_id": "边 ID",
    "scheme_group": "Scheme 分组",
    "step_label": "步骤标签",
    "step_order": "步骤序号",
    "from_mols": "起始分子",
    "to_mols": "目标分子",
    "via_template": "经由模板",
    "via_instance": "经由实例",
    "_multistep": "多步信息",
    "sequence_id": "序列 ID",
    "intermediate_role": "中间体角色",
    "_outcome": "结局标记",
    "is_failed": "是否失败",
    "outcome_text": "失败说明",
    "intended_product_identifier": "本来想做的产物",
    "experiment_type": "实验类型",
    "purpose": "实验目的",
    "reason": "失败原因",
    "ref_in_paper": "论文引用位置",
    "_source": "数据来源",
    "bbox": "bbox 坐标",
    "bbox_index": "bbox 序号",
    "molparser_md5": "MolParser hash",
    "molparser_markush": "Markush 标记",
    "points_to": "指向",
    "from_template": "源模板",
    "from_template_product_index": "源模板产物序号",
    "source_figure": "源 figure",
    "matched_identifier": "匹配 ID",
    "_opus_notes": "处理日志",
    "extraction_strategy": "抽取策略",
    "consultant_calls": "顾问调用",
    "molparser_observations": "MolParser 观察",
    "ambiguities_resolved_by_opus": "歧义解决",
    "questions_for_human_review": "待人工 review",
    "post_audit_fixes": "审计后修复",
    "audit_findings_from_gemini": "Gemini 审计结果",
    "of": "针对产物",  # metrics.of
}


def _label_for_token(t: str) -> str:
    return _FIELD_LABELS_CN.get(t, t)


def _humanize_path(path: str, original: Any, edited: Any) -> str:
    """Walk JSON pointer + the documents to get a chemist-friendly label.

    Example: `/selected_examples/2/products/0/E-SMILES`
        → "实施例 step3-21to22 → 产物 #1 → 结构 (SMILES)"
    """
    if not path or path == "/":
        return "(整体)"
    parts = [t.replace("~1", "/").replace("~0", "~") for t in path.split("/")[1:]]
    out: list[str] = []
    cur_old = original
    cur_new = edited
    for i, tok in enumerate(parts):
        # Try to deref both sides with this token to expose identifier hints
        next_old = next_new = None
        if isinstance(cur_old, dict):
            next_old = cur_old.get(tok)
        elif isinstance(cur_old, list):
            try:
                next_old = cur_old[int(tok)]
            except (ValueError, IndexError):
                next_old = None
        if isinstance(cur_new, dict):
            next_new = cur_new.get(tok)
        elif isinstance(cur_new, list):
            try:
                next_new = cur_new[int(tok)]
            except (ValueError, IndexError):
                next_new = None

        # Token rendering
        if tok.isdigit():
            # If parent dict was a list, prefer identifier-like key from the
            # element if available
            elem = next_new if next_new is not None else next_old
            ident = ""
            if isinstance(elem, dict):
                ident = elem.get("instance_id") or elem.get("template_id") or elem.get("identifier") or ""
            label = f"#{int(tok) + 1}"
            if ident:
                label = f"{ident}({label})"
            out.append(label)
        else:
            out.append(_label_for_token(tok))

        cur_old = next_old
        cur_new = next_new

    return " → ".join(out)


def _group_for_path(path: str) -> str:
    """Top-level grouping for the markdown sections."""
    if path.startswith("/reaction_templates"):
        return "reaction_templates"
    if path.startswith("/selected_examples"):
        return "selected_examples"
    if path.startswith("/reaction_graph"):
        return "reaction_graph"
    if path.startswith("/synthesis_overview"):
        return "synthesis_overview"
    if path.startswith("/identifier_map"):
        return "identifier_map"
    if path.startswith("/texts"):
        return "texts"
    if path.startswith("/_dropped"):
        return "_dropped"
    if path.startswith("/_meta"):
        return "_meta"
    if path.startswith("/_opus_notes"):
        return "_opus_notes"
    if path.startswith("/is_multistep_synthesis"):
        return "synthesis_overview"
    return "其它"


_GROUP_TITLE_CN = {
    "reaction_templates": "模板 (reaction_templates)",
    "selected_examples": "实施例 (selected_examples)",
    "reaction_graph": "反应 DAG (reaction_graph)",
    "synthesis_overview": "多步合成总览 (synthesis_overview)",
    "identifier_map": "标识符映射 (identifier_map)",
    "texts": "文本 (texts)",
    "_dropped": "标记非反应",
    "_meta": "元信息",
    "_opus_notes": "抽取日志 (_opus_notes)",
    "其它": "其它字段",
}


# ── reaction_graph special rendering ───────────────────────────────────


def _render_edge_summary(edge: Any) -> str:
    """``"step 3 · 6 → 7 (via T_step1)"`` for one reaction_graph entry dict."""
    if not isinstance(edge, dict):
        return _short_value(edge)
    step = edge.get("step_order")
    src = edge.get("from_mols") or []
    dst = edge.get("to_mols") or []
    via = edge.get("via_template") or "?"
    arrow = f"{', '.join(src) or '?'} → {', '.join(dst) or '(failed)'}"
    return f"step {step or '?'} · `{arrow}` (via `{via}`)"


def _render_reaction_graph_op(op: dict, original: Any, edited: Any) -> str | None:
    """Specialised rendering for reaction_graph mutations.

    Returns markdown string, or None to fall back to generic renderer.
    """
    path = op.get("path", "")
    # Match exactly /reaction_graph/<i> (a whole edge add/remove)
    parts = path.split("/")
    if len(parts) == 3 and parts[1] == "reaction_graph" and parts[2].isdigit():
        if op["op"] == "add":
            return f"- 新增反应边: {_render_edge_summary(op.get('new'))}"
        if op["op"] == "remove":
            return f"- 删除反应边: {_render_edge_summary(op.get('old'))}"
        if op["op"] == "replace":
            return (
                f"- 替换反应边: {_render_edge_summary(op.get('old'))}"
                f" → {_render_edge_summary(op.get('new'))}"
            )
    # Field-level diff inside an edge: e.g. /reaction_graph/2/from_mols
    if len(parts) >= 4 and parts[1] == "reaction_graph" and parts[2].isdigit():
        edge_idx = int(parts[2])
        field = parts[3]
        # Pull the edge dict for context
        try:
            edge_ctx = (edited.get("reaction_graph") or [])[edge_idx]
            if not isinstance(edge_ctx, dict):
                edge_ctx = (original.get("reaction_graph") or [])[edge_idx]
        except (IndexError, KeyError, AttributeError, TypeError):
            edge_ctx = {}
        edge_label = _render_edge_summary(edge_ctx)
        sub_label = _label_for_token(field)
        if op["op"] == "replace":
            return (
                f"- 修改反应边 {edge_label} 的 **{sub_label}**: "
                f"{_short_value(op.get('old'))} → {_short_value(op.get('new'))}"
            )
        if op["op"] == "add":
            return f"- 反应边 {edge_label} 新增字段 **{sub_label}**: {_short_value(op.get('new'))}"
        if op["op"] == "remove":
            return f"- 反应边 {edge_label} 删除字段 **{sub_label}** (原值 {_short_value(op.get('old'))})"
    return None


# ── _outcome.is_failed special rendering ───────────────────────────────


def _render_outcome_op(op: dict, original: Any, edited: Any) -> str | None:
    path = op.get("path", "")
    parts = path.split("/")

    def _parent_label(section: str, idx: int) -> str | None:
        try:
            parent = (edited.get(section) or [])[idx] or (original.get(section) or [])[idx]
            ident = parent.get("template_id") or parent.get("instance_id") or f"#{idx + 1}"
            kind = "模板" if section == "reaction_templates" else "实施例"
            return f"{kind} **{ident}**"
        except (IndexError, KeyError, TypeError, ValueError):
            return None

    # Case A: replace on /<section>/<i>/_outcome/is_failed
    if "/_outcome/is_failed" in path and op.get("op") == "replace":
        if len(parts) >= 5 and parts[3] == "_outcome":
            label = _parent_label(parts[1], int(parts[2]))
            if label:
                if op.get("new") is True:
                    return f"- 标记 {label} 为 ✗ 失败反应"
                if op.get("new") is False:
                    return f"- 取消 {label} 的失败标记 (恢复为正常)"

    # Case B: add of the entire _outcome dict (template never had it before)
    if (
        len(parts) == 4
        and parts[3] == "_outcome"
        and op.get("op") == "add"
        and isinstance(op.get("new"), dict)
    ):
        label = _parent_label(parts[1], int(parts[2]))
        new_oc = op.get("new") or {}
        if label and new_oc.get("is_failed") is True:
            extra = ""
            if new_oc.get("outcome_text"):
                extra = f" — {new_oc['outcome_text']}"
            elif new_oc.get("note"):
                extra = f" — {new_oc['note']}"
            return f"- 新标记 {label} 为 ✗ 失败反应{extra}"
        if label and new_oc.get("experiment_type"):
            return f"- 新标记 {label} 为控制实验 (`{new_oc['experiment_type']}`)"

    # Case C: remove of the entire _outcome dict (failed → cleared)
    if (
        len(parts) == 4
        and parts[3] == "_outcome"
        and op.get("op") == "remove"
    ):
        label = _parent_label(parts[1], int(parts[2]))
        old_oc = op.get("old") or {}
        if label and old_oc.get("is_failed"):
            return f"- 取消 {label} 的失败标记 (清除整个 _outcome 块)"

    return None


def compute_markdown_diff(original: Any, edited: Any) -> str:
    ops = compute_json_diff(original, edited)
    if not ops:
        return "**没有改动。**\n"

    # Group ops by top-level section
    grouped: dict[str, list[dict]] = {}
    for op in ops:
        g = _group_for_path(op.get("path", "/"))
        grouped.setdefault(g, []).append(op)

    out: list[str] = []
    out.append(f"# 审核 diff · {len(ops)} 处改动\n")

    section_order = [
        "_dropped",
        "synthesis_overview",
        "reaction_graph",
        "reaction_templates",
        "selected_examples",
        "identifier_map",
        "texts",
        "_opus_notes",
        "_meta",
        "其它",
    ]
    for g in section_order:
        if g not in grouped:
            continue
        out.append(f"\n## {_GROUP_TITLE_CN.get(g, g)} · {len(grouped[g])} 处\n")
        for op in grouped[g]:
            # Try v2 special renderers first (reaction_graph edges, _outcome flips)
            special = _render_reaction_graph_op(op, original, edited)
            if special is None:
                special = _render_outcome_op(op, original, edited)
            if special is not None:
                out.append(special)
                continue
            label = _humanize_path(op.get("path", "/"), original, edited)
            kind = op["op"]
            if kind == "replace":
                out.append(
                    f"- 修改 **{label}**:{_short_value(op.get('old'))} → {_short_value(op.get('new'))}"
                )
            elif kind == "add":
                out.append(f"- 新增 **{label}**:{_short_value(op.get('new'))}")
            elif kind == "remove":
                out.append(f"- 删除 **{label}**(原值 {_short_value(op.get('old'))})")
            else:
                out.append(f"- {kind} {label}")
    out.append("")
    return "\n".join(out)
