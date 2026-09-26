"""Markush wildcard substitution for E-SMILES.

Takes a SMILES with `<a>idx:label</a>` annotations like:
    CC1C=CC(N(*)C)=CC=1<sep><a>6:p-Tol</a>

And substitutes each `*` with the SMILES fragment for its annotation label.
The substitution dictionary maps label → SMILES fragment.
Markush enumeration from the example overrides annotation defaults.

Example:
    smi = 'CC1C=CC(N(*)C)=CC=1<sep><a>6:p-Tol</a>'
    markush = {'R1': 'Mes'}  # overrides default
    result = substitute(smi, markush)  # → 'CC1C=CC(N(...mesityl SMILES...)C)=CC=1'
"""
import re
from rdkit import Chem


# Master substituent dictionary (consolidated from all per-paper fix scripts).
SUB_SMILES = {
    # Common aryl groups
    'n-Pent': 'CCCCC',
    'SCH3': 'SC',
    'Ph': 'c1ccccc1',
    'phenyl': 'c1ccccc1',
    'phenyl (Ph)': 'c1ccccc1',
    'p-Tol': 'c1ccc(C)cc1',
    'Tol': 'c1ccc(C)cc1',
    '4-Me-C6H4': 'c1ccc(C)cc1',
    'Mes': 'c1c(C)cc(C)cc1C',
    'mesityl': 'c1c(C)cc(C)cc1C',
    '1-Naphthyl': 'c1cccc2ccccc12',
    '2-Naphthyl': 'c2ccc1ccccc1c2',
    '2-naphthyl': 'c2ccc1ccccc1c2',
    'Bzh': 'C(c1ccccc1)c1ccccc1',
    'benzhydryl': 'C(c1ccccc1)c1ccccc1',
    'PMP': 'c1ccc(OC)cc1',
    'Ar': 'c1ccccc1',                # generic aryl → use phenyl as proxy
    # Halophenyl
    '4-F-C6H4': 'c1ccc(F)cc1',
    'p-F-C6H4': 'c1ccc(F)cc1',
    '4-Cl-C6H4': 'c1ccc(Cl)cc1',
    'p-Cl-C6H4': 'c1ccc(Cl)cc1',
    '4-Br-C6H4': 'c1ccc(Br)cc1',
    'p-Br-C6H4': 'c1ccc(Br)cc1',
    '4-I-C6H4': 'c1ccc(I)cc1',
    'p-I-C6H4': 'c1ccc(I)cc1',
    'p-MeO-C6H4': 'c1ccc(OC)cc1',
    '4-MeO-C6H4': 'c1ccc(OC)cc1',
    'p-OMe-C6H4': 'c1ccc(OC)cc1',
    '4-NO2-C6H4': 'c1ccc([N+](=O)[O-])cc1',
    'p-NO2-C6H4': 'c1ccc([N+](=O)[O-])cc1',
    '4-CN-C6H4': 'c1ccc(C#N)cc1',
    'p-CN-C6H4': 'c1ccc(C#N)cc1',
    'p-SMe-C6H4': 'c1ccc(SC)cc1',
    '4-SMe-C6H4': 'c1ccc(SC)cc1',
    'm-Br-C6H4': 'c1cccc(Br)c1',
    'p-Bpin-C6H4': 'c1ccc(B2OC(C)(C)C(C)(C)O2)cc1',
    # Heteroaryl
    '2-thienyl': 'c1cccs1',
    '2-furyl': 'c1ccco1',
    '6-chloro-2-pyridyl': 'c1cccc(Cl)n1',
    'pyrrole': 'n1cccc1',
    'pyrazole': 'n1cccn1',
    'indole': 'n1ccc2ccccc12',
    'carbazole': 'n1c2ccccc2c2ccccc12',
    # Alkyl
    'H': '[H]',
    'Me': 'C',
    'CH3': 'C',
    'Et': 'CC',
    'CH2CH3': 'CC',
    'n-Pr': 'CCC',
    'nPr': 'CCC',
    'i-Pr': 'C(C)C',
    'iPr': 'C(C)C',
    'n-Bu': 'CCCC',
    'nBu': 'CCCC',
    'tBu': 'C(C)(C)C',
    't-Bu': 'C(C)(C)C',
    's-Bu': 'C(C)CC',  'sBu': 'C(C)CC',  'sec-Bu': 'C(C)CC',
    'i-Bu': 'CC(C)C',  'iBu': 'CC(C)C',  'isobutyl': 'CC(C)C',
    'n-Pent': 'CCCCC',  'nPent': 'CCCCC',
    'n-Hex': 'CCCCCC',  'nHex': 'CCCCCC',  'hexyl': 'CCCCCC',
    'n-Hept': 'CCCCCCC', 'nHept': 'CCCCCCC',
    'n-Oct': 'CCCCCCCC', 'nOct': 'CCCCCCCC',  'octyl': 'CCCCCCCC',
    'n-Non': 'CCCCCCCCC',
    'n-Dec': 'CCCCCCCCCC',  'decyl': 'CCCCCCCCCC',
    'n-Dodec': 'CCCCCCCCCCCC',  'dodecyl': 'CCCCCCCCCCCC',
    # Common alkyl shortcuts
    'allyl': 'CC=C',
    'vinyl': 'C=C',
    'propargyl': 'CC#C',
    'cPr': 'C1CC1',  'cyclopropyl': 'C1CC1',
    'cBu': 'C1CCC1',  'cyclobutyl': 'C1CCC1',
    'cPent': 'C1CCCC1',  'cyclopentyl': 'C1CCCC1',
    'Cy': 'C1CCCCC1',
    'cyclohexyl': 'C1CCCCC1',
    'cHex': 'C1CCCCC1',
    'Bn': 'Cc1ccccc1',
    'CH2Ph': 'Cc1ccccc1',
    'CH2SiMe3': 'C[Si](C)(C)C',
    'SiMe3': '[Si](C)(C)C',
    'TMS': '[Si](C)(C)C',
    'CH2SiMe2Ph': 'C[Si](C)(C)c1ccccc1',
    'CH2CF3': 'CC(F)(F)F',
    # Heterocyclic / functional
    'CO2Me': 'C(=O)OC',
    'B(OH)2': 'B(O)O',
    'Bpin': 'B2OC(C)(C)C(C)(C)O2',
    'CH2CH2OMe': 'COCC',
    # ───── Common functional group labels (commonly emitted by MolParser ─────
    # in <a>idx:label</a> annotations when a *-atom in the SMILES skeleton
    # actually represents a specific functional group rather than a Markush
    # placeholder. Added 2026-05-28 to make MolParser's chemistry encoding
    # round-trip correctly through finalize (was being stripped before).
    'CO2H': 'C(=O)O',  'COOH': 'C(=O)O',  'CO2-': 'C(=O)[O-]',
    'CO2Et': 'C(=O)OCC',  'CO2tBu': 'C(=O)OC(C)(C)C',  'CO2Bn': 'C(=O)OCc1ccccc1',
    'CONH2': 'C(=O)N',  'CONHMe': 'C(=O)NC',  'CONMe2': 'C(=O)N(C)C',
    'CHO': 'C=O',       'COMe': 'C(=O)C',  'COCH3': 'C(=O)C',  'COEt': 'C(=O)CC',
    'OH': 'O',          'OMe': 'OC',       'OEt': 'OCC',       'OBn': 'OCc1ccccc1',
    'OAc': 'OC(=O)C',   'OTs': 'OS(=O)(=O)c1ccc(C)cc1',
    'OTf': 'OS(=O)(=O)C(F)(F)F',  'OBz': 'OC(=O)c1ccccc1',
    'NH2': 'N',         'NH': 'N',         'NHMe': 'NC',       'NMe2': 'N(C)C',
    'NHEt': 'NCC',      'NHBn': 'NCc1ccccc1',
    'NHBoc': 'NC(=O)OC(C)(C)C',  'NBoc': 'NC(=O)OC(C)(C)C',
    'NHCbz': 'NC(=O)OCc1ccccc1',  'NHTs': 'NS(=O)(=O)c1ccc(C)cc1',
    'NO2': '[N+](=O)[O-]',  'NO': 'N=O',
    'SH': 'S',  'SMe': 'SC',  'SEt': 'SCC',  'SPh': 'Sc1ccccc1',
    'SO2Me': 'S(=O)(=O)C',  'SO2Ph': 'S(=O)(=O)c1ccccc1',
    'SO3H': 'S(=O)(=O)O',   'SO2NH2': 'S(=O)(=O)N',
    'Ts': 'S(=O)(=O)c1ccc(C)cc1',
    'CN': 'C#N',         'NC': '[N+]#[C-]',
    'F': 'F',  'Cl': 'Cl',  'Br': 'Br',  'I': 'I',
    'CF3': 'C(F)(F)F',   'CCl3': 'C(Cl)(Cl)Cl',  'CBr3': 'C(Br)(Br)Br',
    # ───── Common atoms / small functional groups (B-16 R3.4) ─────
    'N3': 'N=[N+]=[N-]',  'azide': 'N=[N+]=[N-]',  '-N3': 'N=[N+]=[N-]',
    'CH2': 'C',  # bare methylene → treated as CH2 attachment
    # MolParser often emits aryl variants with no hyphen (e.g. "2-MeC6H4")
    # alongside the canonical "2-Me-C6H4". Add the unhyphenated forms.
    '2-MeC6H4':  'c1ccccc1C',          '2-Me-C6H4': 'c1ccccc1C',
    '3-MeC6H4':  'c1cccc(C)c1',        '3-Me-C6H4': 'c1cccc(C)c1',
    '4-MeC6H4':  'c1ccc(C)cc1',        # alias of p-Tol
    '4-FC6H4':   'c1ccc(F)cc1',        # alias of 4-F-C6H4
    '4-ClC6H4':  'c1ccc(Cl)cc1',
    '4-BrC6H4':  'c1ccc(Br)cc1',
    '4-OMeC6H4': 'c1ccc(OC)cc1',       '4-MeOC6H4': 'c1ccc(OC)cc1',
    '4-CF3C6H4': 'c1ccc(C(F)(F)F)cc1', '4-CF3-C6H4': 'c1ccc(C(F)(F)F)cc1',
    '4-NO2C6H4': 'c1ccc([N+](=O)[O-])cc1',
    '4-CNC6H4':  'c1ccc(C#N)cc1',
    'OCF3': 'OC(F)(F)F',
    'CO2R': 'C(=O)OC',  # CO2R typically treated as ester
    'Boc': 'C(=O)OC(C)(C)C',  'Cbz': 'C(=O)OCc1ccccc1',  'Fmoc': 'C(=O)OCC1c2ccccc2-c2ccccc21',
    'Ac': 'C(=O)C',  'Bz': 'C(=O)c1ccccc1',
    # Phosphorus
    'PO(OMe)2': 'P(=O)(OC)OC',  'P(O)(OMe)2': 'P(=O)(OC)OC',
    'PO(OEt)2': 'P(=O)(OCC)OCC',  'P(O)(OEt)2': 'P(=O)(OCC)OCC',
    # ───── Silicon protecting groups (B-16 R3.2) ─────
    'TBS': '[Si](C)(C)C(C)(C)C',      'TBDMS': '[Si](C)(C)C(C)(C)C',
    'TIPS': '[Si](C(C)C)(C(C)C)C(C)C', 'TES': '[Si](CC)(CC)CC',
    'TBDPS': '[Si](C(C)(C)C)(c1ccccc1)c1ccccc1',
    'SiMe2Ph': '[Si](C)(C)c1ccccc1',  'DMTS': '[Si](C)(C)c1ccccc1',
    'SiPh3': '[Si](c1ccccc1)(c1ccccc1)c1ccccc1',
    # ───── Boron protecting / leaving groups (B-16 R3.2) ─────
    # BMIDA = MIDA boronate (N-methyl iminodiacetic acid boronate)
    'BMIDA': 'B12OC(=O)CN(C)CC(=O)O1',
    'MIDA':  'B12OC(=O)CN(C)CC(=O)O1',  # historically context-equivalent
    'Bdan':  'B1Nc2ccccc2N1',  # naphthalenediaminoborane
    'BF3K':  '[B-](F)(F)F.[K+]',
    'BF3-':  '[B-](F)(F)F',
    # ───── Organometallic groups (B-16 R3.2) ─────
    # ZrCp[2] / Cp2Zr / ZrCp2Cl = zirconocene chloride substituent
    'ZrCp2Cl': '[Zr](Cl)(C1=CC=CC1)C2=CC=CC2',
    'ZrCp[2]': '[Zr](Cl)(C1=CC=CC1)C2=CC=CC2',
    'Cp2Zr': '[Zr](C1=CC=CC1)C2=CC=CC2',
    'ZrCp': '[Zr](C1=CC=CC1)C2=CC=CC2',
    'Cp':    'C1=CC=CC1',  # cyclopentadienyl
    'Cp*':   'C1=C(C)C(C)=C(C)C1C',  # pentamethyl-Cp
    'Fc':    '[Fe]C1=CC=CC1.C1=CC=CC1',  # ferrocenyl (approximate; bound to one Cp)
    'ferrocenyl': '[Fe]C1=CC=CC1.C1=CC=CC1',
    # ───── Common N-protecting groups (B-16 R3.2) ─────
    'Cbz-NH': 'NC(=O)OCc1ccccc1',  'PMB': 'Cc1ccc(OC)cc1',  'DMB': 'Cc1cc(OC)cc(OC)c1',
    'MOM': 'OCOC',  'BOM': 'OCOCc1ccccc1',  'SEM': 'CC[Si](C)(C)CCOC',
    # Markush PLACEHOLDERS (NOT to be expanded by expand_chemistry_labels —
    # but kept here as a reference; the expand function checks MARKUSH_LABELS).
    # Don't add R / R1 / R[N] / PG / Ar entries — they should stay as `*`.
}


# Labels that represent genuine R-group placeholders, NOT chemistry. These
# stay as `*` after expand_chemistry_labels. Used to distinguish e.g.
# `<a>3:R[2]</a>` (a Markush variable to be resolved by row enumeration)
# vs `<a>4:CO2H</a>` (an actual carboxylic acid drawn as `*` in the figure).
MARKUSH_PLACEHOLDER_LABELS = {
    'R', "R'", "R''", "R'''", "R''''", 'PG', 'Ar', 'X', 'Y', 'Z',
    'N_heteroaryl', 'subst', 'arene', 'het', 'het_Ar',
    'CO2R', 'OR',  # CO2R can be both — but as a Markush placeholder it varies
}
# Plus all R1/R2/R[1]/R[2]/... handled programmatically below.


# Annotation regex: <a>idx:label</a> or <r>idx:label</r>
ANN_RE = re.compile(r'<([ar])>(\d+):([^<]+)</[ar]>')


# Two-letter SMILES atoms (so the token-walk in expand_chemistry_labels
# counts atoms correctly when stepping through e.g. "Cl" or "Br").
_TWO_CHAR_ELEMENTS = {'Cl', 'Br', 'Si', 'Se', 'Hg', 'As', 'Ge', 'Sn', 'Te', 'Sb'}


def _is_markush_placeholder(label: str) -> bool:
    """True if the label is a Markush variable (stays as `*`), False if it's
    a chemistry group that should be expanded to actual SMILES.

    MolParser uses `<a>idx:label</a>` for both kinds — we have to distinguish:
      - `<a>0:R[1]</a>`  → Markush placeholder (atom stays as `*`)
      - `<a>3:PG</a>`    → Markush placeholder (PG = generic protecting group)
      - `<a>4:CO2H</a>`  → chemistry group (atom expands to `C(=O)O`)
    """
    if not label:
        return True
    s = label.strip()
    if s in MARKUSH_PLACEHOLDER_LABELS:
        return True
    # R-style: R, R1, R2, R[1], R[2], R', R'' ...
    if re.match(r"^R'*\[?\d*\]?$", s):
        return True
    # X-style positional vars: X1, Y2, Z3 (but only when followed by digit)
    if re.match(r"^[XYZ]\d*$", s):
        return True
    # Generic substituent placeholders
    if s.lower() in ('r-group', 'substituent', 'subst.', 'r_group'):
        return True
    return False


_ORGANIC_BARE_OK = {
    'B', 'C', 'N', 'O', 'P', 'S', 'F', 'Cl', 'Br', 'I',
    'b', 'c', 'n', 'o', 'p', 's', '*',
}


def _strip_atom_map_nums(smi: str) -> str:
    """Remove `:N` map-num suffixes from bracketed atoms, dropping brackets
    when the bare token (e.g. `C`, `c`, `*`) is valid unbracketed SMILES.

    Example: `[*:1][C@H](N[*:2])C(=O)O` → `*[C@H](N*)C(=O)O`
             `[N+:1](=O)[O-]`           → `[N+](=O)[O-]`
    """
    def _replace(m: re.Match) -> str:
        content = m.group(1)
        if content in _ORGANIC_BARE_OK:
            return content
        return f'[{content}]'
    return re.sub(r'\[([^\]]+?):\d+\]', _replace, smi)


def expand_chemistry_labels(smi_with_ann: str) -> tuple[str, int]:
    """Replace `*`-atoms whose `<a>idx:label</a>` annotation names a real
    functional group (CO2H, OMe, NH2, OAc, NHBoc, NO2, ...) with the
    expanded SMILES, via RDKit RWMol graph operations.

    Atoms whose annotation is a Markush placeholder (R, R[1], PG, Ar, X, ...)
    remain as `*` — their atom_idx is updated to match the canonicalised body.

    Robustness wins from the RDKit-based implementation (vs. the earlier
    token-walker):
      - Start-of-fragment substituents (`*c1...<a>0:OMe</a>` → `COc1...`,
        not the invalid `(OC)c1...`).
      - Heteroatom dedup: when the parent atom is N/O/S and the substituent's
        first atom is the same element, the substituent's first atom is
        dropped (`*[C@H](N*)<a>3:NHBoc</a>` → R-N-Boc, not R-N-N-Boc).
      - Bracketed/charged substituents (`[N+](=O)[O-]`) attach correctly at
        any position.
      - Markush atoms survive canonicalisation via atom-map-num tracking.

    Returns:
      (expanded_smiles_with_sep_and_remaining_markush_annotations, n_expanded)
    """
    if not smi_with_ann or '<a>' not in smi_with_ann:
        return smi_with_ann, 0
    body, anns = parse_annotations(smi_with_ann)
    if not anns:
        return smi_with_ann, 0

    chem_anns: list[dict] = []
    keep_anns: list[dict] = []
    for ann in anns:
        if ann['tag'] != 'a':
            keep_anns.append(ann)
            continue
        label = ann['label'].strip()
        if _is_markush_placeholder(label) or label not in SUB_SMILES:
            keep_anns.append(ann)
            continue
        chem_anns.append(ann)

    if not chem_anns:
        return smi_with_ann, 0

    try:
        from rdkit import RDLogger
        RDLogger.DisableLog('rdApp.*')
    except ImportError:
        return smi_with_ann, 0

    body_mol = Chem.MolFromSmiles(body)
    if body_mol is None:
        return smi_with_ann, 0

    rwmol = Chem.RWMol(body_mol)

    # Tag Markush dummies (and any <r> real-atom annotations we want to keep)
    # with atom map nums so they survive RDKit canonicalisation. We will strip
    # the map nums from the final SMILES after recording the new atom indices.
    map_num_to_ann: dict[int, dict] = {}
    next_map_num = 1
    for ann in keep_anns:
        ai = ann['atom_idx']
        if ai >= rwmol.GetNumAtoms():
            continue
        atom = rwmol.GetAtomWithIdx(ai)
        if ann['tag'] == 'a' and atom.GetAtomicNum() != 0:
            continue  # <a> annotation expects a dummy; refuse to mis-tag
        atom.SetAtomMapNum(next_map_num)
        map_num_to_ann[next_map_num] = ann
        next_map_num += 1

    dummies_to_delete: set[int] = set()
    n_expanded = 0

    for ann in chem_anns:
        ai = ann['atom_idx']
        if ai >= rwmol.GetNumAtoms():
            continue
        atom = rwmol.GetAtomWithIdx(ai)
        if atom.GetAtomicNum() != 0:
            continue
        neighbors = list(atom.GetNeighbors())
        if len(neighbors) != 1:
            continue
        parent_idx = neighbors[0].GetIdx()
        parent_elem = neighbors[0].GetSymbol()
        body_bond = rwmol.GetBondBetweenAtoms(ai, parent_idx)
        body_bond_type = body_bond.GetBondType()

        sub_smi = SUB_SMILES[ann['label'].strip()]
        sub_mol = Chem.MolFromSmiles(sub_smi)
        if sub_mol is None or sub_mol.GetNumAtoms() == 0:
            continue

        first_sub_atom = sub_mol.GetAtomWithIdx(0)
        first_sub_elem = first_sub_atom.GetSymbol()

        # Heteroatom dedup. Real example: parent N + NHBoc (= NC(=O)OC(C)(C)C).
        # Without dedup the result is R-N-NHBoc (hydrazide); with dedup it's
        # R-N-Boc (Boc-protected amine), the chemically intended form.
        if (parent_elem in ('N', 'O', 'S')
                and first_sub_elem == parent_elem
                and sub_mol.GetNumAtoms() > 1):
            anchor_targets = []
            for nb in first_sub_atom.GetNeighbors():
                nb_bond = sub_mol.GetBondBetweenAtoms(0, nb.GetIdx())
                anchor_targets.append((nb.GetIdx(), nb_bond.GetBondType()))
            sub_rwmol = Chem.RWMol(sub_mol)
            sub_rwmol.RemoveAtom(0)
            sub_clean = sub_rwmol.GetMol()
            anchor_targets_shifted = [(idx - 1, bt) for idx, bt in anchor_targets]

            offset = rwmol.GetNumAtoms()
            rwmol = Chem.RWMol(Chem.CombineMols(rwmol, sub_clean))
            for nb_idx, nb_bt in anchor_targets_shifted:
                rwmol.AddBond(parent_idx, offset + nb_idx, nb_bt)
        else:
            offset = rwmol.GetNumAtoms()
            rwmol = Chem.RWMol(Chem.CombineMols(rwmol, sub_mol))
            rwmol.AddBond(parent_idx, offset, body_bond_type)

        dummies_to_delete.add(ai)
        n_expanded += 1

    if not dummies_to_delete:
        return smi_with_ann, 0

    for d in sorted(dummies_to_delete, reverse=True):
        rwmol.RemoveAtom(d)

    try:
        Chem.SanitizeMol(rwmol)
    except Exception:
        return smi_with_ann, 0

    smi_with_maps = Chem.MolToSmiles(rwmol)
    if not smi_with_maps:
        return smi_with_ann, 0

    # Re-parse the canonical SMILES so atom indices match the token order in
    # the emitted string. Atom map nums survive parsing, so we can locate the
    # Markush atoms in their new canonical positions.
    parsed = Chem.MolFromSmiles(smi_with_maps)
    if parsed is None:
        return smi_with_ann, 0

    new_anns: list[dict] = []
    for atom in parsed.GetAtoms():
        mn = atom.GetAtomMapNum()
        if mn > 0 and mn in map_num_to_ann:
            orig = map_num_to_ann[mn]
            new_anns.append({
                'tag': orig['tag'],
                'atom_idx': atom.GetIdx(),
                'label': orig['label'],
            })

    clean_smi = _strip_atom_map_nums(smi_with_maps)

    tail_parts = [f"<{a['tag']}>{a['atom_idx']}:{a['label']}</{a['tag']}>"
                  for a in new_anns]
    if tail_parts:
        return clean_smi + '<sep>' + ''.join(tail_parts), n_expanded
    if '<sep>' in smi_with_ann:
        return clean_smi + '<sep>', n_expanded
    return clean_smi, n_expanded


def parse_annotations(smi_with_ann):
    """Parse the annotation tail of a Markush E-SMILES.
    Returns (body_smiles, annotations) where annotations is a list of
    dicts {tag: 'a'|'r', atom_idx: int, label: str}."""
    body, _, tail = smi_with_ann.partition('<sep>')
    anns = []
    for m in ANN_RE.finditer(tail):
        anns.append({'tag': m.group(1), 'atom_idx': int(m.group(2)), 'label': m.group(3)})
    return body, anns


_R_GROUP_ALIASES = ['R', 'R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R[1]', 'R[2]', 'R[3]', 'R[4]', 'R[5]', 'Ar', 'X', 'Y', 'Z']


def _extract_baseline_default(v, fallback):
    """If v looks like 'baseline (X)' extract X; if v == 'baseline' return fallback."""
    if not isinstance(v, str):
        return v
    m = re.search(r'baseline\s*\(([^)]+)\)', v)
    if m:
        return m.group(1).strip()
    if v == 'baseline':
        return fallback
    return v


def resolve_label(label, markush_enum):
    """Given an annotation label and the example's markush_enumeration, resolve to a substituent name.
    markush_enum keys are typically like 'R', 'R1', 'R2', 'X', or specific label names."""
    # Direct hit
    if label in markush_enum:
        return _extract_baseline_default(markush_enum[label], label)
    # Without brackets
    label_alt = label.replace('[', '').replace(']', '')
    if label_alt in markush_enum:
        return _extract_baseline_default(markush_enum[label_alt], label_alt)
    # Generic single-letter variable (X, Y, Z) → find the ONE non-baseline markush value
    # The annotation typically encodes a position the example varies. If markush_enum has
    # exactly one non-baseline value, assume that's the substitution.
    if label in ('X', 'Y', 'Z', 'Ar', 'R'):
        non_baseline = [(k, v) for k, v in markush_enum.items()
                        if isinstance(v, str) and 'baseline' not in v and v != 'baseline']
        if len(non_baseline) == 1:
            return non_baseline[0][1]
    # Label itself may BE the substituent value (e.g. annotation 'p-Tol' is the actual default)
    return label


_SMILES_VALID_CHARS = set('CNOPSFIBrClcnops()=#-+0123456789@/\\[].')


# ----- name → SMILES disk cache (so repeated lookups don't hammer remote APIs) -----
import os as _os
import json as _json
_OPSIN_CACHE_DIR = _os.path.expanduser('~/.cache/chemreaction_benchmark')
_OPSIN_CACHE_FILE = _os.path.join(_OPSIN_CACHE_DIR, 'name_to_smi.json')
_OPSIN_CACHE = None  # type: ignore  # populated by _load_cache()
# Opt-in: only hit remote services if user sets this. Default OFF so unit tests
# / offline runs stay deterministic. Enable via env var or programmatically.
_REMOTE_LOOKUP_ENABLED = _os.environ.get('CHEMBENCH_REMOTE_NAME_LOOKUP', '0') == '1'


def enable_remote_name_lookup(enabled: bool = True):
    """Toggle OPSIN/PubChem/CIR remote lookups on `substituent_to_smi` misses."""
    global _REMOTE_LOOKUP_ENABLED
    _REMOTE_LOOKUP_ENABLED = enabled


def _load_cache():
    global _OPSIN_CACHE
    if _OPSIN_CACHE is None:
        try:
            with open(_OPSIN_CACHE_FILE, 'r') as f:
                _OPSIN_CACHE = _json.load(f)
        except Exception:
            _OPSIN_CACHE = {}
    return _OPSIN_CACHE


def _save_cache():
    if _OPSIN_CACHE is None:
        return
    try:
        _os.makedirs(_OPSIN_CACHE_DIR, exist_ok=True)
        with open(_OPSIN_CACHE_FILE, 'w') as f:
            _json.dump(_OPSIN_CACHE, f, indent=2, sort_keys=True)
    except Exception:
        pass


def _remote_name_to_smi(name):
    """Try OPSIN → PubChem → CIR for IUPAC/common name → SMILES.

    Cached on disk to disambiguate repeat lookups across runs. Returns None
    if every service fails or the response can't be RDKit-validated.
    """
    cache = _load_cache()
    if name in cache:
        return cache[name] or None  # cached None stored as empty string

    try:
        import requests
    except ImportError:
        return None

    candidates = []
    try:
        from urllib.parse import quote
        encoded = quote(name, safe='')
    except Exception:
        encoded = name

    # OPSIN — fastest, free, IUPAC-only
    try:
        r = requests.get(f'https://opsin.ch.cam.ac.uk/opsin/{encoded}.smi', timeout=5)
        if r.ok and r.text.strip():
            candidates.append(('opsin', r.text.strip().split()[0]))
    except Exception:
        pass
    # PubChem — broader (common names, IUPAC, drug names)
    if not candidates:
        try:
            r = requests.get(
                f'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{encoded}/property/CanonicalSMILES/TXT',
                timeout=5)
            if r.ok and r.text.strip():
                candidates.append(('pubchem', r.text.strip().split('\n')[0].strip()))
        except Exception:
            pass
    # CIR (Cactus) — final fallback
    if not candidates:
        try:
            r = requests.get(f'https://cactus.nci.nih.gov/chemical/structure/{encoded}/smiles', timeout=5)
            if r.ok and r.text.strip() and 'not found' not in r.text.lower():
                candidates.append(('cir', r.text.strip().split('\n')[0].strip()))
        except Exception:
            pass

    # Validate the SMILES with RDKit
    for source, smi in candidates:
        try:
            from rdkit import RDLogger
            RDLogger.DisableLog('rdApp.*')
            m = Chem.MolFromSmiles(smi)
            if m is not None:
                canon = Chem.MolToSmiles(m)
                cache[name] = canon
                _save_cache()
                return canon
        except Exception:
            continue
    # Cache the miss
    cache[name] = ""
    _save_cache()
    return None


def substituent_to_smi(name):
    """Resolve substituent NAME to a SMILES fragment WITHOUT the attach-point `*`.

    GT markush_enumeration values come in three forms:
      (a) leading-star fragment SMILES — `*c1ccccc1`, `*C`, `*[H]`
          (the `*` marks where the fragment attaches; strip it for substitution)
      (b) leading-star + `//` comment — `*  // pyrrole`, `*  // 4-NO2-C6H4`
          (try the comment as a name in SUB_SMILES, then RDKit/OPSIN)
      (c) bare name — `p-Tol`, `Bn`, `cyclohexyl` (legacy from older skill docs)
      (d) bare SMILES already — `c1ccccc1`, `C`

    Lookup chain:
      1. SUB_SMILES dict (local, ~80 substituents) — keys are bare names
      2. RDKit parse (if string already looks like SMILES)
      3. OPSIN → PubChem → CIR (if CHEMBENCH_REMOTE_NAME_LOOKUP=1)

    Returns the canonical SMILES of the fragment, ready to be inserted at a
    `*` position in the template body. Returns None if not resolvable.
    """
    if name is None:
        return None
    name = name.strip() if isinstance(name, str) else name
    if not name or len(name) > 200:
        return None

    # ----- form (b): `*  // <comment>` — comment is the actual substituent label
    m_comment = re.match(r'^\*\s*//\s*(.+?)\s*$', name)
    if m_comment:
        inner = m_comment.group(1).strip()
        # Recurse, but cap depth (inner should be a bare name or SMILES)
        return substituent_to_smi(inner)

    # ----- form (a): `*<smi>` — strip leading `*` (attach point), keep rest
    if name.startswith('*') and len(name) > 1 and name[1] != '*':
        smi_after = name[1:].lstrip()
        if smi_after:
            try:
                from rdkit import RDLogger
                RDLogger.DisableLog('rdApp.*')
                m = Chem.MolFromSmiles(smi_after)
                if m is not None:
                    return Chem.MolToSmiles(m)
            except Exception:
                pass
        return None

    # Bare-`*` alone (placeholder, no info) — cannot resolve
    if name == '*':
        return None

    # ----- form (c): bare-name dictionary
    if name in SUB_SMILES:
        return SUB_SMILES[name]

    # ----- form (c'): molparserutils 12,631-entry dict (local-table-first, so known keys above are
    # byte-identical; this only ADDS coverage — Bpin/Boc/Ts/PMP etc.). No-op unless USE_MOLPARSERUTILS=1
    # and molparser_utils is importable (server 3.11); returns bare SMILES.
    try:
        from agent import abbrev_ext
        ext = abbrev_ext.bare_smi(name)
        if ext:
            return ext
    except Exception:
        pass

    # Reject obvious placeholder labels (R, X1, R[5], ...)
    if all(ch in 'RXrx0123456789[]' for ch in name):
        return None

    # ----- form (d): looks like SMILES (no spaces / special phrases)
    if not any(ch in name for ch in (' ', ',', 'fused', 'spiro', 'bridge', 'derived')):
        try:
            from rdkit import RDLogger
            RDLogger.DisableLog('rdApp.*')
            m = Chem.MolFromSmiles(name)
            if m is not None:
                return Chem.MolToSmiles(m)
        except Exception:
            pass

    # ----- form (c) extended: remote name → SMILES fallback (OPSIN/PubChem/CIR)
    if _REMOTE_LOOKUP_ENABLED:
        if any(ch in name for ch in ('fused', 'spiro', 'bridge', 'derived')):
            return None
        return _remote_name_to_smi(name)
    return None


def substitute(smi_with_ann, markush_enum=None, ring_label_offset=10):
    """Substitute Markush wildcards in an E-SMILES with concrete SMILES fragments.
    Returns (resolved_smiles, substitutions_dict) where substitutions_dict records
    {atom_idx: {label, value, smi_fragment}} for each substitution.

    Strategy: convert `*` to `[*:N]` with isotope tag, use RDKit ReplaceSubstructs
    or atom-level rewriting. Simpler approach: do string replacement of `*` in
    SMILES, in left-to-right order matching the annotations' atom_idx order.

    Caveat: ring labels in substituent fragments need to be re-indexed to avoid
    collisions with main-ring labels. Use ring_label_offset to bump fragment
    ring labels (e.g., c1ccccc1 → c11ccccc11 if main uses ring 1)."""
    if markush_enum is None:
        markush_enum = {}
    body, anns = parse_annotations(smi_with_ann)
    if not anns:
        return smi_with_ann, {'substitutions': [], 'note': 'no annotations'}

    # Sort by atom_idx (asc); the n-th annotation corresponds to the n-th `*` in body
    anns_sorted = sorted(anns, key=lambda a: a['atom_idx'])

    # Substitution decisions
    subs = []
    n_unresolved = 0
    for ann in anns_sorted:
        label = ann['label']
        resolved_name = resolve_label(label, markush_enum)
        sub_smi = substituent_to_smi(resolved_name)
        if sub_smi is None:
            sub_smi = '*'  # fallback: keep as wildcard
            n_unresolved += 1
        subs.append({'atom_idx': ann['atom_idx'], 'label': label,
                     'resolved_name': resolved_name, 'sub_smi': sub_smi})
    # If ALL substituents were unresolved (no vocab match), return None — caller may keep original
    if n_unresolved == len(subs) and len(subs) > 0:
        return None, {'error': 'no_substituents_resolved', 'subs': subs,
                      'original': smi_with_ann, 'substitutions': subs}

    # Bump ring labels in substituents to avoid collisions
    def bump_ring_labels(s, offset):
        # Replace digit X (1..9) with (X+offset). Conservative: only single-digit ring labels.
        # Use %NN notation when offset+X > 9.
        out = []
        for ch in s:
            if ch.isdigit():
                new = int(ch) + offset
                if new > 9:
                    out.append('%' + f'{new:02d}')
                else:
                    out.append(str(new))
            else:
                out.append(ch)
        return ''.join(out)

    # Find max ring label in body to know offset
    max_ring = 0
    for ch in body:
        if ch.isdigit():
            max_ring = max(max_ring, int(ch))

    # Replace * one at a time, with per-substitution offset
    result = body
    used_subs = []
    cur_offset = max_ring
    for sub in subs:
        # Bump sub_smi's ring labels by cur_offset
        bumped = bump_ring_labels(sub['sub_smi'], cur_offset)
        # Find the FIRST `*` in current result (assumes order matches sub order)
        idx = result.find('*')
        if idx < 0:
            break
        result = result[:idx] + bumped + result[idx+1:]
        used_subs.append({**sub, 'sub_smi_bumped': bumped})
        # Update cur_offset to avoid collisions with this sub's ring labels
        for ch in bumped:
            if ch.isdigit():
                cur_offset = max(cur_offset, int(ch))

    # Canonicalize via RDKit
    try:
        from rdkit import RDLogger
        RDLogger.DisableLog('rdApp.*')
        mol = Chem.MolFromSmiles(result)
        if mol is None:
            return None, {'error': 'RDKit cannot parse substituted SMILES', 'attempted': result, 'substitutions': used_subs}
        canon = Chem.MolToSmiles(mol)
    except Exception as e:
        return None, {'error': str(e), 'attempted': result, 'substitutions': used_subs}

    return canon, {'substitutions': used_subs, 'attempted_smiles': result,
                   'canonical': canon, 'original': smi_with_ann}


# =====================================================================
# Graph-level R-group substitution (RDKit RWMol + CombineMols).
#
# Mimics the spirit of ChemEagle's _convert_graph_to_smiles but uses RDKit
# directly. Used as Stage-5 fallback when string substitute fails (ring-label
# collisions, complex stereochemistry, atomic placeholders the string path
# can't handle).
# =====================================================================


def _is_h_substituent(name):
    """True if the resolved substituent name means 'just hydrogen' — e.g.
    `H`, `*H`, `[H]`, `*[H]`, or any of these with whitespace.
    Used by graph_substitute to short-circuit R=H by deleting the body dummy."""
    if name is None or not isinstance(name, str):
        return False
    s = name.strip()
    if not s:
        return False
    return s in ('H', '*H', '[H]', '*[H]', '* H', '* [H]')


def _resolve_to_attached_smi(name):
    """Return SMILES with a leading `*` atom marking the attach point —
    the form needed for graph_substitute. Returns None if not resolvable.

    Differences from substituent_to_smi:
      - Always adds back the leading `*` (graph_substitute needs the attach point)
      - Falls back to OPSIN/PubChem only if remote enabled
    """
    if name is None:
        return None
    name = name.strip() if isinstance(name, str) else name
    if not name or len(name) > 200:
        return None

    # Form: `*  // <comment>` — pull comment, resolve that, prepend *
    m_comment = re.match(r'^\*\s*//\s*(.+?)\s*$', name)
    if m_comment:
        inner = m_comment.group(1).strip()
        sub = substituent_to_smi(inner)  # bare SMILES, no leading *
        if sub:
            return '*' + sub
        return None

    # Form: `*<smi>` — already in graph-attached form, verify and return
    if name.startswith('*') and len(name) > 1 and name[1] != '*':
        body = name[1:].lstrip()
        if body:
            try:
                from rdkit import RDLogger
                RDLogger.DisableLog('rdApp.*')
                m = Chem.MolFromSmiles('*' + body)
                if m is not None:
                    return Chem.MolToSmiles(m)
            except Exception:
                pass
        return None

    # Bare-* alone is just a placeholder, cannot graph-substitute
    if name == '*':
        return None

    # Bare name → SUB_SMILES dict → prepend *
    bare = substituent_to_smi(name)
    if bare:
        if bare.startswith('['):
            # Single-atom substituent like [H] or [Cl]
            return '*' + bare
        return '*' + bare
    return None


def graph_substitute(smi_with_ann, markush_enum=None):
    """RDKit RWMol + CombineMols Markush substitution.

    For each `<a>idx:label</a>` (or `<r>`) annotation in the template:
      1. Find the dummy atom at SMILES atom_idx
      2. Resolve label → substituent SMILES (form `*<smi>`)
      3. Combine substituent mol with body mol
      4. Add bond from body-anchor to sub-anchor (preserve original bond type)
      5. Remove both dummies in one final pass

    Returns:
      (canonical_smiles, meta_dict) — same shape as substitute()

    Advantages over string substitute():
      - No ring-label collision issues (RDKit re-canonicalizes)
      - Preserves bond order/stereo from template
      - Handles `[Me]`, `[OMe]` atomic placeholders that string can't
    """
    if markush_enum is None:
        markush_enum = {}

    body, anns = parse_annotations(smi_with_ann)
    if not anns:
        return smi_with_ann, {'substitutions': [], 'note': 'no annotations'}

    try:
        from rdkit import RDLogger
        RDLogger.DisableLog('rdApp.*')
    except ImportError:
        return None, {'error': 'rdkit not available'}

    body_mol = Chem.MolFromSmiles(body)
    if body_mol is None:
        return None, {'error': 'body SMILES does not parse', 'body': body}

    # B-16 R7: MolParser's atom indices in <a>idx:label</a> sometimes count
    # explicit [H] atoms (e.g. `[C@]([H])(*)*`) that RDKit's MolFromSmiles drops,
    # shifting every downstream index. When a tag-'a' annotation no longer points
    # at a dummy atom, the indices are misaligned — re-map the tag-'a' annotations
    # onto the body's REAL dummy atoms positionally. Both MolParser and RDKit
    # traverse the SMILES left-to-right, so the sorted order is preserved. <r>
    # annotations legitimately point to real ring atoms, so they're untouched.
    _a_anns = [a for a in anns if a['tag'] == 'a']
    _dummy_idxs = sorted(at.GetIdx() for at in body_mol.GetAtoms()
                         if at.GetAtomicNum() == 0)
    _misaligned = any(
        a['atom_idx'] >= body_mol.GetNumAtoms()
        or body_mol.GetAtomWithIdx(a['atom_idx']).GetAtomicNum() != 0
        for a in _a_anns
    )
    if _misaligned and _dummy_idxs and len(_a_anns) == len(_dummy_idxs):
        for _a, _real in zip(sorted(_a_anns, key=lambda x: x['atom_idx']), _dummy_idxs):
            _a['atom_idx'] = _real

    rwmol = Chem.RWMol(body_mol)
    body_dummy_indices = set()      # original atoms to delete at the end
    combined_dummy_indices = set()  # post-combine atoms to delete
    pending_bonds = []              # (body_anchor_idx, sub_anchor_idx, bond_type)
    resolutions = []                # for telemetry / debugging

    for ann in anns:
        ann_idx = ann['atom_idx']
        if ann_idx >= rwmol.GetNumAtoms():
            return None, {'error': f'atom_idx {ann_idx} out of range',
                          'n_atoms': rwmol.GetNumAtoms(), 'body': body}

        ann_atom = rwmol.GetAtomWithIdx(ann_idx)

        if ann['tag'] == 'a':
            # <a>idx:label</a> — idx points to the dummy atom itself.
            if ann_atom.GetAtomicNum() != 0:
                return None, {'error': f'<a> at idx {ann_idx} is '
                                        f'{ann_atom.GetSymbol()!r}, expected dummy *'}
            neighbors = list(ann_atom.GetNeighbors())
            if len(neighbors) != 1:
                return None, {'error': f'body dummy at {ann_idx} has '
                                        f'{len(neighbors)} neighbors (expected 1)'}
            body_anchor_idx = neighbors[0].GetIdx()
            body_bond = rwmol.GetBondBetweenAtoms(ann_idx, body_anchor_idx)
            body_bond_type = body_bond.GetBondType()
            body_dummy_to_delete = ann_idx
        elif ann['tag'] == 'r':
            # <r>idx:label</r> — idx points to a real ring atom that bears an
            # IMPLICIT substituent (not drawn as a dummy). We add a new bond
            # from this atom to the substituent; no body atom to delete.
            if ann_atom.GetAtomicNum() == 0:
                return None, {'error': f'<r> at idx {ann_idx} is dummy * (expected real atom)'}
            body_anchor_idx = ann_idx
            body_bond_type = Chem.BondType.SINGLE  # implicit ring substituents are single bonds
            body_dummy_to_delete = None  # nothing to delete on body side
        else:
            return None, {'error': f'unknown annotation tag {ann["tag"]!r}'}

        # Resolve label
        resolved_name = resolve_label(ann['label'], markush_enum)

        # B-16 R3.1: `R=*H` / `R=H` / `R=[H]` means "substituent is hydrogen" —
        # the chemically correct expansion is to delete the body dummy and
        # let the parent carry an implicit H. No combine step needed.
        if _is_h_substituent(resolved_name):
            if body_dummy_to_delete is not None:
                body_dummy_indices.add(body_dummy_to_delete)
            resolutions.append({'label': ann['label'], 'tag': ann['tag'],
                                'resolved_name': resolved_name,
                                'sub_smi': '[H]', 'mode': 'delete-dummy'})
            continue

        sub_smi = _resolve_to_attached_smi(resolved_name)
        if sub_smi is None:
            return None, {'error': f'cannot resolve label {ann["label"]!r} '
                                    f'(resolved name {resolved_name!r})',
                          'resolutions': resolutions}

        sub_mol = Chem.MolFromSmiles(sub_smi)
        if sub_mol is None:
            return None, {'error': f'sub SMILES does not parse: {sub_smi!r}'}

        # Find sub dummy
        sub_dummies = [a.GetIdx() for a in sub_mol.GetAtoms() if a.GetAtomicNum() == 0]
        if len(sub_dummies) != 1:
            return None, {'error': f'sub {sub_smi!r} has {len(sub_dummies)} dummies, need exactly 1'}
        sub_dummy_idx = sub_dummies[0]
        sub_dummy_atom = sub_mol.GetAtomWithIdx(sub_dummy_idx)
        sub_neighbors = list(sub_dummy_atom.GetNeighbors())
        if len(sub_neighbors) == 0:
            return None, {'error': f'sub {sub_smi!r} has lone dummy (no anchor)'}
        if len(sub_neighbors) > 1:
            return None, {'error': f'sub {sub_smi!r} dummy has multi-neighbors'}
        sub_anchor_idx = sub_neighbors[0].GetIdx()

        # CombineMols — sub atoms get offset
        offset = rwmol.GetNumAtoms()
        rwmol = Chem.RWMol(Chem.CombineMols(rwmol, sub_mol))

        sub_dummy_combined = offset + sub_dummy_idx
        sub_anchor_combined = offset + sub_anchor_idx

        if body_dummy_to_delete is not None:
            body_dummy_indices.add(body_dummy_to_delete)
        combined_dummy_indices.add(sub_dummy_combined)
        pending_bonds.append((body_anchor_idx, sub_anchor_combined, body_bond_type))
        resolutions.append({'label': ann['label'], 'tag': ann['tag'],
                            'resolved_name': resolved_name, 'sub_smi': sub_smi})

    # Add all bonds
    for a, b, t in pending_bonds:
        rwmol.AddBond(a, b, t)

    # Delete all dummies — sorted descending so removal doesn't shift earlier indices
    all_dummies = sorted(body_dummy_indices | combined_dummy_indices, reverse=True)
    for d in all_dummies:
        rwmol.RemoveAtom(d)

    try:
        Chem.SanitizeMol(rwmol)
        canon = Chem.MolToSmiles(rwmol)
        return canon, {'method': 'graph',
                       'substitutions': resolutions,
                       'canonical': canon,
                       'original': smi_with_ann}
    except Exception as e:
        return None, {'error': f'RDKit sanitize/canonicalize fail: {e}',
                      'resolutions': resolutions}


def substitute_best_effort(smi_with_ann, markush_enum=None):
    """Try graph substitute first (more chemically robust); fall back to string.

    Order chosen because graph approach (RDKit RWMol) handles:
      - <r>idx:label</r> ring-implicit substituents that string can't see
      - multi-* templates with ring-label collisions
      - bond order / stereo preservation

    string substitute is the fallback for cases where:
      - graph fails to resolve substituent name (string has same lookup, but no error)
      - body SMILES doesn't parse cleanly with RDKit (legacy MolParser quirks)

    Returns:
      (canonical_smiles, meta_dict). meta['method'] ∈ {'graph', 'string', 'all_failed'}.
    """
    # 1. Try graph
    canon_g, meta_g = graph_substitute(smi_with_ann, markush_enum)
    if canon_g and not meta_g.get('error') and '*' not in canon_g:
        meta_g['method'] = 'graph'
        return canon_g, meta_g

    # 2. Fall back to string
    canon_s, meta_s = substitute(smi_with_ann, markush_enum)
    if canon_s and not meta_s.get('error') and '*' not in canon_s:
        meta_s['method'] = 'string'
        return canon_s, meta_s

    # 3. Both failed
    return None, {'method': 'all_failed',
                  'graph_meta': meta_g, 'string_meta': meta_s,
                  'original': smi_with_ann}


if __name__ == '__main__':
    # Quick self-test
    tests = [
        ('CC1C=CC(N(*)C)=CC=1<sep><a>6:p-Tol</a>', {}),
        ('CC1C=CC(N(*)C)=CC=1<sep><a>6:p-Tol</a>', {'R1': 'Mes'}),  # only resolves if annotation label matches
        ('[Si](C)(C)(C)CC1(C2C(=CC=C(C=2)C)N(*)CC1)C<sep><a>14:p-Tol</a>', {}),
        ('*c1ccc(N(*)C(C2C=CC(*)=CC=2)C(*)(*))cc1<sep><a>0:R</a><a>5:R[1]</a><a>11:R[2]</a><a>15:R[3]</a><a>16:R[4]</a>',
         {'R': '6-Me', 'R1': 'p-Tol', 'R2': 'H', 'R3': 'Me', 'R4': 'CH2SiMe3'}),
    ]
    for smi, m in tests:
        result, meta = substitute(smi, m)
        print(f'input:  {smi}')
        print(f'markush: {m}')
        print(f'result: {result}')
        print(f'meta error: {meta.get("error")}')
        print()
