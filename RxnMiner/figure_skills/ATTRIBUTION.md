# Attribution

## third_party/molecular_assembly, third_party/molecular_assembly_v2
R-group **decomposition / assembly / reverse-assemble** library from the upstream
molecular-assembly project. This is the code that:
- extracts R-groups from a drawn concrete against a Markush template (R-group "reverse"),
- fills a Markush template with R-groups to materialize a concrete molecule (R-group substitution / fill),
- and, at the high level, `reverse_assemble(target_product, product_template, [reactant_templates])`
  derives a reaction's undrawn reactants in one call (see `src/chemistry/reverse_assembly.py`).

Included here with permission under the original upstream terms. Preserve this attribution
and the upstream license terms when redistributing. Source of record (read-only):
`upstream source archive`.
`molecular_assembly_v2` is the newer copy with better aryl ring-substituent handling.

## engine/
`engine/markush.py` and `engine/v2.py` are OURS. `markush.py` is the thin wrapper that shows exactly
HOW the workbench calls the assembly library: `reverse_r` (reverse), `forward_fill` (fill), and `reverse_assemble_reactants`
(the high-level derivation, prefers molecular_assembly_v2). Read it to see the R-group flow end to end.

## OCSR
Structure reading is done by MolParser (a remote OCSR service), invoked behind the `detect` tool in
`tools/chem_mcp.py`. The client + endpoint are not included here.
