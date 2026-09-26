"""engine.rdkit_ops — self-contained RDKit helpers for `canon` and `transform`.
No repo dependency (RDKit only). These are exactly what `tool.py canon` / `tool.py
transform` run.
"""
from __future__ import annotations

import re
from typing import List

from rdkit import Chem, RDLogger
from rdkit.Chem import AllChem

RDLogger.DisableLog("rdApp.*")

_TAG = re.compile(r"<a>[^<]*</a>|<r>[^<]*</r>")


def strip_esmiles(esmiles: str) -> str:
    """E-SMILES -> bare SMILES: drop <a>/<r> tags, <sep>->'.', <dum>->'*'."""
    if not esmiles:
        return ""
    s = _TAG.sub("", esmiles)
    s = s.replace("<sep>", ".").replace("<dum>", "*")
    return s.strip(". ").strip()


def canon(smiles_or_esmiles: str, stereo: bool = True) -> str:
    """RDKit canonical SMILES (isomeric by default). '' if invalid."""
    s = strip_esmiles(smiles_or_esmiles)
    if not s:
        return ""
    m = Chem.MolFromSmiles(s)
    if m is None:
        return ""
    return Chem.MolToSmiles(m, isomericSmiles=stereo)


def transform(rxn_smarts: str, *reactants: str):
    """Apply an RDKit reaction SMARTS with ANY number of reactant templates -> products.

    `reactants` = one SMILES per reactant template, IN TEMPLATE ORDER. The reaction may
    produce any number of products. Covers 1->1, 2->1, 1->2, N->M:
      transform('[c:1][OX2H1:2]>>[c:1]B(O)O', 'COc1ccc(O)cc1')              # 1->1 (phenol<-boronic acid)
      transform('[C:1]=O.[C:3]=[C:4]>>[C:1]1[C:3][C:4]1', carbonyl, alkene)  # 2->1 (cyclopropanation)
      transform('[C:1][OX2H1:2]>>[C:1]=O.[O:2]', diol)                       # 1->2 (gives product-tuples)
    As a convenience, a single dot-joined string is split to fill a multi-template reaction
    (`transform(smarts, 'A.B')` == `transform(smarts, 'A', 'B')`).

    RETURNS: a FLAT list of product SMILES when every match yields exactly one product
    (the common case, back-compatible); otherwise a list of product-TUPLES (one list per
    match, M SMILES each). You choose the SMARTS (the chemistry); RDKit keeps outputs valid."""
    rxn = AllChem.ReactionFromSmarts(rxn_smarts)
    n_t = rxn.GetNumReactantTemplates()
    smis = list(reactants)
    if len(smis) == 1 and n_t > 1 and "." in smis[0]:
        smis = smis[0].split(".")
    if len(smis) != n_t:
        raise ValueError(f"reaction SMARTS needs {n_t} reactant(s); got {len(smis)}: {smis}")
    mols = []
    for s in smis:
        m = Chem.MolFromSmiles(s)
        if m is None:
            raise ValueError(f"bad SMILES: {s}")
        mols.append(m)
    seen: List[list] = []
    for tup in rxn.RunReactants(tuple(mols)):
        prods, ok = [], True
        for p in tup:
            try:
                Chem.SanitizeMol(p)
                prods.append(Chem.MolToSmiles(p))
            except Exception:
                ok = False
        if ok and prods and prods not in seen:
            seen.append(prods)
    if all(len(x) == 1 for x in seen):
        return [x[0] for x in seen]
    return seen
