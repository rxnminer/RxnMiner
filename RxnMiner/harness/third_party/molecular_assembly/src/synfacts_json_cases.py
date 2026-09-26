"""Load Step0 cases from Synfacts *-with-examples.json (shared by batch + server)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


def esmiles(item: dict) -> str:
    return (item.get("E-SMILES") or item.get("E_SMILES") or "").strip()


def product_identifier(item: dict) -> str:
    return (item.get("identifier") or "").strip()


def product_scaffolds(products: list[dict]) -> list[str]:
    return [esmiles(p) for p in products if esmiles(p)]


def example_products(products: list[dict]) -> list[dict]:
    """Example-side product entries that carry E-SMILES (same order as scaffolds)."""
    return [p for p in products if esmiles(p)]


def resolve_product_identifier(
    products: list[dict],
    product_index: int,
    *,
    instance_id: str = "",
    template_identifier: str = "",
) -> str:
    """Prefer example product identifier (e.g. ``3a``); then instance_id; then template."""
    items = example_products(products)
    if items:
        item = items[product_index] if product_index < len(items) else items[-1]
        ident = product_identifier(item)
        if ident:
            return ident
    if instance_id:
        return instance_id
    if template_identifier:
        return template_identifier
    return ""


@dataclass(frozen=True)
class TemplateProduct:
    scaffold: str
    identifier: str


def load_template_products_map(data: dict) -> dict[str, list[TemplateProduct]]:
    """template_id -> ordered template products (scaffold + JSON identifier)."""
    out: dict[str, list[TemplateProduct]] = {}
    for tpl in data.get("reaction_templates") or []:
        tid = (tpl.get("template_id") or "").strip()
        if not tid:
            continue
        entries: list[TemplateProduct] = []
        for item in tpl.get("products") or []:
            scaffold = esmiles(item)
            if scaffold:
                entries.append(
                    TemplateProduct(scaffold=scaffold, identifier=product_identifier(item))
                )
        if entries:
            out[tid] = entries
    return out


def load_template_product_map(data: dict) -> dict[str, list[str]]:
    """template_id -> ordered product scaffold lines (E-SMILES with <sep>)."""
    return {
        tid: [entry.scaffold for entry in entries]
        for tid, entries in load_template_products_map(data).items()
    }


@dataclass
class JsonCase:
    json_file: str
    example_index: int
    product_index: int
    product_identifier: str = ""
    template_id: str = ""
    scaffold_line: str = ""
    target_smiles: str = ""
    error: str = ""
    instance_id: str = ""
    yield_text: str = ""
    markush_enumeration: dict[str, str] = field(default_factory=dict)
    reactant_esmiles: list[str] = field(default_factory=list)

    @property
    def case_key(self) -> str:
        return f"{self.example_index}:{self.product_index}"

    @property
    def valid(self) -> bool:
        return not self.error and bool(self.target_smiles) and bool(self.scaffold_line)

    def product_label(self) -> str:
        if self.product_index < 0:
            return "—"
        if self.product_identifier:
            return f"产物{self.product_identifier}"
        return f"产物{self.product_index}"

    def label(self, *, max_smi: int = 42) -> str:
        if self.error:
            return f"例{self.example_index} · 错误: {self.error[:60]}"
        smi = self.target_smiles
        if len(smi) > max_smi:
            smi = smi[: max_smi - 1] + "…"
        prod = self.product_label()
        tid = self.template_id or "?"
        yld = f" · {self.yield_text}" if self.yield_text else ""
        return f"例{self.example_index} · {prod} · {tid}{yld} · {smi}"


def _yield_from_example(ex: dict) -> str:
    for m in ex.get("metrics") or []:
        if (m.get("role") or "").strip() == "yield":
            return (m.get("text") or "").strip()
    prods = ex.get("products") or []
    if prods:
        return (prods[0].get("description") or "").strip()
    return ""


def iter_json_cases(data: dict, json_name: str) -> list[JsonCase]:
    """One case per (selected_example, product_index) pair — same as batch_decompose_json."""
    tpl_map = load_template_products_map(data)
    cases: list[JsonCase] = []

    for ex_i, ex in enumerate(data.get("selected_examples") or []):
        inherit = (ex.get("inherit_template") or "").strip()
        instance_id = (ex.get("instance_id") or "").strip()
        yield_text = _yield_from_example(ex)
        markush = dict(ex.get("markush_enumeration") or {})
        reactant_lines = [
            esmiles(r) for r in (ex.get("reactants") or []) if esmiles(r)
        ]
        targets = product_scaffolds(ex.get("products") or [])

        if not inherit:
            cases.append(
                JsonCase(
                    json_file=json_name,
                    example_index=ex_i,
                    product_index=-1,
                    instance_id=instance_id,
                    yield_text=yield_text,
                    markush_enumeration=markush,
                    reactant_esmiles=reactant_lines,
                    error="missing inherit_template",
                )
            )
            continue

        template_products = tpl_map.get(inherit)
        if not template_products:
            cases.append(
                JsonCase(
                    json_file=json_name,
                    example_index=ex_i,
                    product_index=-1,
                    template_id=inherit,
                    instance_id=instance_id,
                    yield_text=yield_text,
                    markush_enumeration=markush,
                    reactant_esmiles=reactant_lines,
                    target_smiles=targets[0] if targets else "",
                    error=f"no template products for {inherit}",
                )
            )
            continue

        if not targets:
            cases.append(
                JsonCase(
                    json_file=json_name,
                    example_index=ex_i,
                    product_index=-1,
                    template_id=inherit,
                    instance_id=instance_id,
                    yield_text=yield_text,
                    markush_enumeration=markush,
                    reactant_esmiles=reactant_lines,
                    scaffold_line=template_products[0].scaffold,
                    error="example has no product E-SMILES",
                )
            )
            continue

        n = max(len(template_products), len(targets))
        for pi in range(n):
            tpl_prod = (
                template_products[pi]
                if pi < len(template_products)
                else template_products[-1]
            )
            scaffold = tpl_prod.scaffold
            target = targets[pi] if pi < len(targets) else targets[-1]
            prod_ident = resolve_product_identifier(
                ex.get("products") or [],
                pi,
                instance_id=instance_id,
                template_identifier=tpl_prod.identifier,
            )
            err = ""
            if "<sep>" not in scaffold:
                err = "scaffold_line missing <sep>"
            cases.append(
                JsonCase(
                    json_file=json_name,
                    example_index=ex_i,
                    product_index=pi,
                    product_identifier=prod_ident,
                    template_id=inherit,
                    scaffold_line=scaffold,
                    target_smiles=target,
                    instance_id=instance_id,
                    yield_text=yield_text,
                    markush_enumeration=markush,
                    reactant_esmiles=reactant_lines,
                    error=err,
                )
            )
    return cases


def load_json_cases(path: Path) -> list[JsonCase]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return iter_json_cases(data, path.name)


def safe_json_filename(name: str) -> str:
    """Reject path traversal; require *-with-examples.json basename."""
    base = Path(name).name
    if base != name or ".." in base:
        raise ValueError("invalid json filename")
    if not base.endswith("-with-examples.json"):
        raise ValueError("filename must end with -with-examples.json")
    return base


def list_json_files(json_dir: Path, *, q: str = "", limit: int = 80) -> list[str]:
    pattern = "*-with-examples.json"
    if q.strip():
        needle = q.strip().lower()
        out: list[str] = []
        for p in sorted(json_dir.glob(pattern)):
            if needle in p.name.lower():
                out.append(p.name)
                if len(out) >= limit:
                    break
        return out
    files = sorted(json_dir.glob(pattern))
    if limit > 0:
        files = files[:limit]
    return [p.name for p in files]
