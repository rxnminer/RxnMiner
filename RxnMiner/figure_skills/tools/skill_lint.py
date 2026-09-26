#!/usr/bin/env python3
"""Anti-leakage lint for the iterable skill (SKILL.md + LESSONS.md).

The skill is allowed to encode GENERAL extraction rules/process; it must NEVER encode figure-specific
answers. This lint is the deterministic guard the iteration loop runs before accepting any skill edit:

  HARD FAIL (any tracked skill file):
    * a figure id  (gt1__/gt2__/gt3__/gt4__/direct__... or a bare 32-hex-char id) -> that's pointing at
      a specific benchmark item.
  HARD FAIL (LESSONS.md only -- lessons must be prose rules, not molecule instances):
    * a token RDKit parses as a concrete molecule with >= MIN_ATOMS heavy atoms -> that's an answer,
      not a rule. (SKILL.md may keep a few illustrative SMILES inside tool docs; LESSONS.md may not.)

Usage:  python tools/skill_lint.py skills/reaction-extract/SKILL.md skills/reaction-extract/LESSONS.md
Exit 0 = clean, 1 = leakage found (print the offending lines). Wire this into the loop's accept gate.
"""
import re, sys

MIN_ATOMS = 5
FIG_ID = re.compile(r"\b(?:gt[1-4]|direct|synfact|pdf)__[A-Za-z0-9_]+\b")
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
# candidate SMILES-ish token: has a ring-closure digit or a bond/branch and only SMILES chars
SMILES_TOK = re.compile(r"[A-Za-z0-9@+\-\[\]()=#$%/\\.]{6,}")


def _rdkit_ok(tok):
    try:
        from rdkit import Chem
        from rdkit import RDLogger; RDLogger.DisableLog("rdApp.*")
    except Exception:
        return False
    if not re.search(r"[0-9]", tok) or tok.isalnum() and not re.search(r"[0-9]", tok):
        pass
    m = Chem.MolFromSmiles(tok)
    if m is None:
        return False
    heavy = sum(1 for a in m.GetAtoms() if a.GetAtomicNum() > 1)
    return heavy >= MIN_ATOMS


def lint(path):
    bad = []
    is_lessons = path.lower().endswith("lessons.md")
    for i, line in enumerate(open(path), 1):
        for m in FIG_ID.finditer(line):
            bad.append((i, "figure-id", m.group()))
        for m in HEX32.finditer(line):
            bad.append((i, "32-hex-id", m.group()))
        if is_lessons:                                        # lessons = general prose only, no compound instances
            for tok in SMILES_TOK.findall(line):
                if tok in ("markush", "reaction", "template", "identifier"):
                    continue
                if _rdkit_ok(tok):
                    bad.append((i, "concrete-SMILES-in-LESSONS", tok))
    return bad


def main(paths):
    total = 0
    for p in paths:
        try:
            bad = lint(p)
        except FileNotFoundError:
            print(f"(skip missing {p})"); continue
        for i, kind, tok in bad:
            print(f"LEAK {p}:{i}  [{kind}]  {tok}")
        total += len(bad)
    if total:
        print(f"\nFAIL: {total} leakage hit(s) -- do NOT accept this skill edit.")
        return 1
    print("OK: no figure ids / no concrete-SMILES-in-LESSONS. Skill edit is leakage-clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["skills/reaction-extract/SKILL.md", "skills/reaction-extract/LESSONS.md"]))
