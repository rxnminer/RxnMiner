"""Shared batch Step0 per-JSON processing (CLI + web refresh)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from candidates import candidate_full_pass, candidate_roundtrip_pass
from decompose import step0_scaffold_map
from roundtrip import LibraryContext
from synfacts_json_cases import JsonCase, iter_json_cases


def full_pass_count_for_result(result) -> int:
    return sum(1 for m in result.matches if candidate_full_pass(m))


def roundtrip_pass_count_for_result(result) -> int:
    return sum(1 for m in result.matches if candidate_roundtrip_pass(m))


@dataclass
class CaseResult:
    json_file: str
    example_index: int
    product_index: int
    template_id: str
    scaffold_line: str
    target_smiles: str
    ok: bool = False
    match_count: int = 0
    full_pass_count: int = 0
    roundtrip_pass_count: int = 0
    error: str = ""


@dataclass
class FileSummary:
    json_file: str
    cases: int = 0
    cases_ok: int = 0
    cases_with_full_pass: int = 0
    cases_with_roundtrip_pass: int = 0
    total_full_pass_matches: int = 0
    total_roundtrip_pass_matches: int = 0
    errors: int = 0


def _case_from_json(jc: JsonCase) -> CaseResult:
    return CaseResult(
        json_file=jc.json_file,
        example_index=jc.example_index,
        product_index=jc.product_index,
        template_id=jc.template_id,
        scaffold_line=jc.scaffold_line,
        target_smiles=jc.target_smiles,
        error=jc.error,
    )


def iter_cases(data: dict, json_name: str) -> list[CaseResult]:
    return [_case_from_json(jc) for jc in iter_json_cases(data, json_name)]


def _library_for_case(case: CaseResult, jc: JsonCase | None) -> LibraryContext | None:
    if jc is None:
        return None
    if not jc.markush_enumeration and not jc.reactant_esmiles:
        return None
    return LibraryContext(
        markush_enumeration=dict(jc.markush_enumeration),
        reactant_esmiles=list(jc.reactant_esmiles),
    )


def run_case(
    case: CaseResult,
    *,
    render_images: bool,
    library: LibraryContext | None = None,
) -> CaseResult:
    if case.error:
        return case
    if not case.target_smiles:
        case.error = "empty target_smiles"
        return case
    if not case.scaffold_line:
        case.error = "empty scaffold_line"
        return case
    if "<sep>" not in case.scaffold_line:
        case.error = "scaffold_line missing <sep>"
        return case
    try:
        result = step0_scaffold_map(
            case.target_smiles,
            case.scaffold_line,
            render_images=render_images,
            library=library,
        )
    except Exception as e:
        case.error = str(e)
        return case
    case.ok = result.ok
    case.match_count = result.match_count
    case.full_pass_count = full_pass_count_for_result(result)
    case.roundtrip_pass_count = roundtrip_pass_count_for_result(result)
    return case


def process_json(path: Path, *, render_images: bool) -> FileSummary:
    name = path.name
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    json_cases = iter_json_cases(data, name)
    cases = [_case_from_json(jc) for jc in json_cases]
    jc_by_key = {(jc.example_index, jc.product_index): jc for jc in json_cases}
    summary = FileSummary(json_file=name, cases=len(cases))

    for case in cases:
        jc = jc_by_key.get((case.example_index, case.product_index))
        run_case(case, render_images=render_images, library=_library_for_case(case, jc))
        if case.error:
            summary.errors += 1
            continue
        if case.ok:
            summary.cases_ok += 1
        if case.full_pass_count > 0:
            summary.cases_with_full_pass += 1
        if case.roundtrip_pass_count > 0:
            summary.cases_with_roundtrip_pass += 1
        summary.total_full_pass_matches += case.full_pass_count
        summary.total_roundtrip_pass_matches += case.roundtrip_pass_count

    return summary
