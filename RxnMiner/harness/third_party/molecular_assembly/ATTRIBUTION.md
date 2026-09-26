# Attribution

The code under `third_party/molecular_assembly/` is the `molecular_assembly` R-group
decomposition and assembly engine authored by upstream maintainers, vendored here with permission for
use within this private repository. It provides the low-level scaffold_map + assemble_fragments API used
by `skills/chem_extract/engine/markush.py` to derive a scope cell reactants from the drawn product
and the generic templates.

- Original source (read-only): upstream distribution
- We do not modify the vendored source; wrappers live in `skills/chem_extract/engine/markush.py`.
- The bundled `src/translator/abbrevs*.csv` are the abbreviation-expansion dictionaries that ship
  with the tool.
- Redistribution outside this private repository requires the original authors permission.
