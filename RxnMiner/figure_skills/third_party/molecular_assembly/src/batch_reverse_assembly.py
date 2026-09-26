"""Batch evaluate reverse assembly over _no_batch pages."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Temporary manual skips — remove entries when the case is fixed.
# Keys: (page_index, example_index) or image_name (skips all examples on that page).
TEMPORARY_REVERSE_BATCH_SKIP: frozenset[tuple[int, int] | str] = frozenset(
    {
        (156, 10),  # Synfacts-2021-6-660 entry10 — IndexError in candidate scoring
        # "Synfacts-2021-6-660-with-examples",  # uncomment to skip the whole page
    }
)


def parse_skip_spec(spec: str) -> tuple[int, int] | str:
    """``page:example`` or ``image_name``."""
    if ":" in spec:
        page_s, ex_s = spec.split(":", 1)
        return int(page_s.strip()), int(ex_s.strip())
    return spec.strip()


def merge_skip_sets(
    *sets: frozenset[tuple[int, int] | str] | set[tuple[int, int] | str] | None,
) -> frozenset[tuple[int, int] | str]:
    out: set[tuple[int, int] | str] = set()
    for s in sets:
        if s:
            out.update(s)
    return frozenset(out)


def is_reverse_batch_skipped(
    page_index: int,
    example_index: int,
    image_name: str,
    *,
    skip: frozenset[tuple[int, int] | str] | None = None,
) -> bool:
    rules = skip if skip is not None else TEMPORARY_REVERSE_BATCH_SKIP
    if not rules:
        return False
    if (page_index, example_index) in rules:
        return True
    name = (image_name or "").strip()
    return bool(name and name in rules)

from reverse_assembly import (
    attach_expected_comparisons,
    classify_failure_category,
    example_reactant_smiles_list,
    example_target_smiles,
    example_yield_text,
    iter_example_list_rows,
    matching_official_example,
    pick_detail_from_notes,
    report_status_label,
    reverse_assemble,
    reverse_result_to_dict,
    template_steps_for_id,
)
from reverse_assembly_log import (
    CACHE_VERSION,
    ReverseExampleSummaryRow,
    upsert_reverse_summary_rows,
    write_reverse_cache,
)


def load_official_data_for_page(page_data: dict, *, json_dir: Path) -> dict | None:
    img_name = (page_data.get("image_name") or "").strip()
    if not img_name:
        return None
    name = img_name if img_name.endswith(".json") else f"{img_name}.json"
    path = json_dir / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def comparison_reactants_for_example(
    page_data: dict,
    example: dict,
    *,
    example_index: int,
    official_data: dict | None,
) -> list[str]:
    if official_data is not None:
        official = matching_official_example(
            page_data,
            example,
            official_data,
            example_index=example_index,
        )
        if official is not None:
            reactants = example_reactant_smiles_list(official)
            if reactants:
                return reactants
    return example_reactant_smiles_list(example)


def official_reactant_note(
    example: dict,
    *,
    example_index: int,
    page_data: dict,
    official_data: dict | None,
) -> tuple[bool | None, str]:
    if official_data is None:
        return False, "正式 JSON 无对应实施例"
    official = matching_official_example(
        page_data,
        example,
        official_data,
        example_index=example_index,
    )
    if official is None:
        return False, "正式 JSON 无对应实施例"
    reactants = example_reactant_smiles_list(official)
    if not reactants:
        return False, "正式 JSON 无标注反应物"
    if not example_reactant_smiles_list(example):
        return True, "比对使用正式 JSON 反应物（_no_batch 实施例行未带 reactants）"
    return True, ""


def evaluate_reverse_example(
    page_data: dict,
    *,
    example_index: int,
    page_index: int,
    official_data: dict | None,
    render_images: bool = False,
    write_cache: bool = False,
    cache_dir: Path | None = None,
    skip_rules: frozenset[tuple[int, int] | str] | None = None,
) -> ReverseExampleSummaryRow:
    examples = page_data.get("selected_examples") or []
    if example_index >= len(examples):
        return ReverseExampleSummaryRow(
            image_name=(page_data.get("image_name") or ""),
            page_index=page_index,
            example_index=example_index,
            included=False,
            skip_reason="实施例下标越界",
            status_label="跳过",
            failure_category="skipped",
            detail="实施例下标越界",
        )

    list_rows = {r["example_index"]: r for r in iter_example_list_rows(page_data)}
    row = list_rows.get(example_index) or {}
    image_name = (page_data.get("image_name") or "").strip()
    ex = examples[example_index]

    if is_reverse_batch_skipped(
        page_index, example_index, image_name, skip=skip_rules
    ):
        skip = "临时跳过（batch 配置）"
        label, detail = report_status_label(
            included=False,
            skip_reason=skip,
            assembly_ok=None,
            compare_ok=None,
            stereo_warn=False,
        )
        return ReverseExampleSummaryRow(
            image_name=image_name,
            page_index=page_index,
            example_index=example_index,
            instance_id=row.get("instance_id") or "",
            yield_text=row.get("yield_text") or "",
            included=False,
            skip_reason=skip,
            status_label=label,
            failure_category="skipped",
            detail=detail,
        )

    if not row.get("included"):
        skip = row.get("skip_reason") or "已跳过"
        label, detail = report_status_label(
            included=False,
            skip_reason=skip,
            assembly_ok=None,
            compare_ok=None,
            stereo_warn=False,
        )
        return ReverseExampleSummaryRow(
            image_name=image_name,
            page_index=page_index,
            example_index=example_index,
            instance_id=row.get("instance_id") or "",
            yield_text=row.get("yield_text") or "",
            included=False,
            skip_reason=skip,
            status_label=label,
            failure_category="skipped",
            detail=detail,
        )

    inherit = (ex.get("inherit_template") or "").strip()
    tpl_steps = template_steps_for_id(page_data, inherit)
    scaffold = (tpl_steps["products"] or [""])[0]
    target = example_target_smiles(ex)
    expected = comparison_reactants_for_example(
        page_data,
        ex,
        example_index=example_index,
        official_data=official_data,
    )
    ok_official, list_note = official_reactant_note(
        ex,
        example_index=example_index,
        page_data=page_data,
        official_data=official_data,
    )

    missing_inputs = not (target and scaffold and tpl_steps["reactants"])
    assembly_ok: bool | None = False
    compare_ok: bool | None = None
    stereo_warn = False
    notes: list[str] = []
    result = None

    if missing_inputs:
        detail = list_note or "缺少模板或产物 SMILES"
        notes = [detail]
    else:
        try:
            result = reverse_assemble(
                target,
                scaffold,
                tpl_steps["reactants"],
                render_images=render_images,
                match_index=None,
            )
        except Exception as exc:
            detail = f"逆向组装异常: {type(exc).__name__}: {exc}"
            notes = [detail]
            assembly_ok = False
            compare_ok = None
            label, label_detail = report_status_label(
                included=True,
                skip_reason="",
                assembly_ok=False,
                compare_ok=None,
                stereo_warn=False,
            )
            if not detail:
                detail = label_detail
            return ReverseExampleSummaryRow(
                image_name=image_name,
                page_index=page_index,
                example_index=example_index,
                instance_id=row.get("instance_id") or "",
                yield_text=row.get("yield_text") or "",
                included=True,
                assembly_ok=False,
                compare_ok=None,
                stereo_warn=False,
                status_label=label,
                failure_category="assembly_error",
                detail=detail,
                notes=detail,
            )
        result.template_id = inherit
        result.yield_text = example_yield_text(ex)
        result.markush_enumeration = dict(ex.get("markush_enumeration") or {})
        notes = list(result.notes)
        assembly_ok = result.ok
        if expected:
            compare_ok, _unmatched = attach_expected_comparisons(
                result.reactants,
                expected,
                render_images=render_images,
                notes=[],
            )
            stereo_warn = any(
                "前后不一致" in (r.match_note or "") for r in result.reactants
            )
        elif not ok_official:
            compare_ok = None

    detail = pick_detail_from_notes(notes, list_note=list_note if not ok_official else "")

    label, label_detail = report_status_label(
        included=True,
        skip_reason="",
        assembly_ok=assembly_ok,
        compare_ok=compare_ok,
        stereo_warn=stereo_warn,
    )
    if not detail:
        detail = label_detail

    failure_category = classify_failure_category(
        included=True,
        skip_reason="",
        notes=notes,
        assembly_ok=assembly_ok,
        compare_ok=compare_ok,
        stereo_warn=stereo_warn,
        has_expected=bool(expected),
        missing_inputs=missing_inputs,
    )

    summary = ReverseExampleSummaryRow(
        image_name=image_name,
        page_index=page_index,
        example_index=example_index,
        instance_id=row.get("instance_id") or "",
        yield_text=row.get("yield_text") or "",
        included=True,
        assembly_ok=assembly_ok,
        compare_ok=compare_ok,
        stereo_warn=stereo_warn,
        status_label=label,
        failure_category=failure_category,
        detail=detail,
        notes=" | ".join(notes[:8]),
    )

    if write_cache and result is not None:
        payload: dict[str, Any] = {
            "version": CACHE_VERSION,
            "image_name": image_name,
            "page_index": page_index,
            "example_index": example_index,
            "summary": summary.csv_dict(),
            "reverse": reverse_result_to_dict(result),
        }
        write_reverse_cache(page_index, example_index, payload, cache_dir=cache_dir)

    return summary


def evaluate_reverse_page(
    page_data: dict,
    *,
    page_index: int,
    official_data: dict | None,
    render_images: bool = False,
    write_cache: bool = False,
    cache_dir: Path | None = None,
    skip_rules: frozenset[tuple[int, int] | str] | None = None,
) -> list[ReverseExampleSummaryRow]:
    rows: list[ReverseExampleSummaryRow] = []
    for row in iter_example_list_rows(page_data):
        rows.append(
            evaluate_reverse_example(
                page_data,
                example_index=row["example_index"],
                page_index=page_index,
                official_data=official_data,
                render_images=render_images,
                write_cache=write_cache,
                cache_dir=cache_dir,
                skip_rules=skip_rules,
            )
        )
    return rows


def rerun_reverse_page_cache(
    page_data: dict,
    *,
    page_index: int,
    official_data: dict | None = None,
    json_dir: Path | None = None,
    cache_dir: Path | None = None,
    csv_path: Path | None = None,
    skip_rules: frozenset[tuple[int, int] | str] | None = None,
) -> list[ReverseExampleSummaryRow]:
    """Re-evaluate all examples on one page; overwrite per-example JSON cache + CSV rows."""
    if official_data is None and json_dir is not None:
        official_data = load_official_data_for_page(page_data, json_dir=json_dir)
    rules = skip_rules if skip_rules is not None else TEMPORARY_REVERSE_BATCH_SKIP
    rows = evaluate_reverse_page(
        page_data,
        page_index=page_index,
        official_data=official_data,
        render_images=False,
        write_cache=True,
        cache_dir=cache_dir,
        skip_rules=rules,
    )
    upsert_reverse_summary_rows(rows, csv_path)
    return rows
