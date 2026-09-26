# Contributing

Use a focused branch, describe the problem and resulting behavior, and include the checks you ran.
Prefer descriptive module and function names. Keep service configuration in environment variables.
Do not change extraction prompts or scoring semantics as part of a cosmetic change.

Run `npm test` for the workbench, `python3 -m unittest discover -s tests -v` for the recognition adapter,
and `npm --prefix website run check && npm --prefix website run build` for the website.
Run `python3 scripts/audit_release.py` before sharing a source snapshot.

Identify copied code and assets and retain their notices and original license terms.
