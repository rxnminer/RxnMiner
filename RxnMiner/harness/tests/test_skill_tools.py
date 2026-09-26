"""The chemistry tools the agent calls (skill tool.py verbs) — RDKit + MarkushMap, offline.

These are the deterministic building blocks the agent's tool calls shell out to.
Asserted against the worked examples in docs/INDEX.md."""
import json


def test_canon_benzene(skill_run):
    out, p = skill_run("canon", "C1=CC=CC=C1")
    assert p.returncode == 0
    assert out == "c1ccccc1"                       # Kekule -> aromatic canonical


def test_canon_keeps_stereo(skill_run):
    out, _ = skill_run("canon", "C[C@H](O)CC")
    assert "@" in out                              # stereo preserved


def test_transform_cyclopropanation(skill_run):
    # [C]=O + [C]=[C] >> cyclopropane fused (a real SMARTS transform)
    out, _ = skill_run("transform", "[C:1]=O.[C:3]=[C:4]>>[C:1]1[C:3][C:4]1",
                       "O=Cc1ccccc1", "C=Cc1ccccc1")
    assert "C2CC2" in out                          # cyclopropane ring formed


def test_reverse_extract_r_group(skill_run):
    # read the R substituent out of a concrete instance against a markush template
    out, _ = skill_run("reverse", "*c1ccc(*)cc1<sep><a>0:R</a>", "c1ccc(OC)cc1")
    assert json.loads(out) == {"R": "*OC"}


def test_fill_bare_template_autoannotates(skill_run):
    """fill must auto-annotate a BARE '*' template (no <a> tags) before forward_fill — the agent always passes a
    bare template, so without this fill silently returns '' and the agent abandons the tool (real bug found in
    the 3c00062/149 trajectories). reverse->fill must round-trip to the concrete molecule."""
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "skills" / "chem_extract"))
    from rdkit import Chem
    tmpl = "*C(=O)C1C(C(=O)c2ccccc2)N1c1ccccc1"
    concrete = "O=C(c1ccccc1)C1C(C(=O)c2ccc(OC)cc2)N1c1ccccc1"
    # bare template + the '*'-keyed R that `reverse` returns -> a real concrete SMILES (was '' before the fix)
    out, p = skill_run("fill", tmpl, '{"*":"*c1ccc(OC)cc1"}')
    assert p.returncode == 0 and out and "*" not in out
    assert Chem.MolToSmiles(Chem.MolFromSmiles(out)) == Chem.MolToSmiles(Chem.MolFromSmiles(concrete))
    # an R-labelled key works the same (what the model typically writes)
    out2, _ = skill_run("fill", tmpl, '{"R":"*c1ccc(OC)cc1"}')
    assert Chem.MolToSmiles(Chem.MolFromSmiles(out2)) == Chem.MolToSmiles(Chem.MolFromSmiles(concrete))


def test_validate_batch(skill_run):
    """The BATCH `validate` verb: ONE call canonicalizes+validates a whole SMILES list (replaces per-cell canon
    churn). Per item {input, canonical, valid, has_star}: valid structures canonicalize, junk -> valid=false,
    a '*' template -> has_star=true, stereo is preserved."""
    out, p = skill_run("validate", '["C1=CC=CC=C1", "*c1ccccc1", "not_a_smiles", "C[C@H](O)CC"]')
    assert p.returncode == 0
    rows = json.loads(out)
    assert [r["valid"] for r in rows] == [True, True, False, True]
    assert rows[0]["canonical"] == "c1ccccc1"            # Kekule -> aromatic canonical
    assert rows[1]["has_star"] is True and rows[2]["has_star"] is False
    assert rows[2]["canonical"] == ""                    # invalid -> empty canonical, not a crash
    assert "@" in rows[3]["canonical"]                   # stereo preserved


def test_enumerate_grid_roundtrip_status(monkeypatch):
    """The new BATCH `enumerate` verb: per drawn product -> reverse R -> SELF-CHECK by filling the template back
    (roundtrip_ok) -> fill the template reactant. A clean single-R cell must come back status='trusted' with a
    concrete (no-'*') reactant; this is the deterministic draft the model vets per cell."""
    import sys, pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "skills" / "chem_extract"))
    sys.path.insert(0, str(root / "skills" / "chem_extract_skills"))
    import engine.markush as MK
    monkeypatch.setattr(MK, "reverse_r", MK._rdkit_reverse_r)        # fast deterministic reverse (MarkushMapMulti hangs offline)
    import tool as T
    # clean single-R cell: R = methyl. reverse(*c1ccccc1, Cc1ccccc1)->{R:*C}; roundtrip fills back to Cc1ccccc1
    # (trusted); the *Br reactant fills to CBr.
    res = T._enumerate_grid(["*Br"], ["*c1ccccc1"], ["Cc1ccccc1"])
    assert res["n_total"] == 1 and res["n_trusted"] == 1
    cell = res["cells"][0]
    assert cell["status"] == "trusted" and cell["roundtrip_ok"] is True
    from rdkit import Chem
    fr = cell["filled_reactants"][0]
    assert fr and "*" not in fr and Chem.MolFromSmiles(fr) is not None   # CBr — a real concrete reactant
    # a cell whose reverse mis-reads (no clean roundtrip) must SELF-FLAG as 'failed', not silently mislead
    bad = T._enumerate_grid(["*Br"], ["*c1ccccc1"], ["c1ccc2ccccc2c1"])  # naphthalene: no methyl-style single R
    assert bad["cells"][0]["status"] == "failed"


def test_enumerate_keeps_constant_reactant_on_failed_cell(monkeypatch):
    """A 2-component grid (one varying '*' reactant + one CONSTANT co-reactant) must keep the constant in
    EVERY cell, even when reverse mis-reads the varying part and the cell is status='failed'. This is the
    user-seen bug: the constant co-reactant was dropped from every example. Reproduces ajoc.202200438."""
    import sys, pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "skills" / "chem_extract"))
    sys.path.insert(0, str(root / "skills" / "chem_extract_skills"))
    import engine.markush as MK
    monkeypatch.setattr(MK, "reverse_r", MK._rdkit_reverse_r)
    import tool as T
    tr = ["*/C=C/C(*)=O", "FC(F)/C(Br)=N/Nc1ccccc1"]          # varying enone + constant hydrazonoyl bromide
    tp = ["*C(=O)[C@@H]1[C@@H](*)C(C(F)F)=NN1c1ccccc1"]
    cp = ["Cc1ccc(C(=O)[C@H]2[C@H](c3ccccc3)C(C(F)F)=NN2c2ccccc2)cc1", "not_a_smiles"]
    res = T._enumerate_grid(tr, tp, cp)
    assert res["n_total"] == 2
    for cell in res["cells"]:                                  # the constant survives on every cell incl. junk
        assert cell["filled_reactants"][1] == "FC(F)/C(Br)=N/Nc1ccccc1"
    # a junk product does NOT crash the whole verb (per-cell isolation)
    assert res["cells"][1]["status"] == "failed"


def test_enumerate_constant_when_reverse_empty(monkeypatch):
    """When reverse genuinely cannot derive the markush reactant (scaffold mismatch: dict-keyed reverse_r AND
    the positional reverse_multi both fail to match), the markush reactant comes back None but the CONSTANT
    reactant is still carried through -- so the manual fallback never loses the co-reactant."""
    import sys, pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "skills" / "chem_extract"))
    sys.path.insert(0, str(root / "skills" / "chem_extract_skills"))
    import engine.markush as MK
    monkeypatch.setattr(MK, "reverse_r", lambda *a, **k: {})  # reverse reads nothing
    import tool as T
    # concrete product is a DIFFERENT scaffold (pyridine) than the template (benzene) -> reverse_multi also
    # returns None (true mismatch), exercising the constant-passthrough on a genuinely-failed cell.
    res = T._enumerate_grid(["*Br", "ClCCl"], ["*c1ccccc1"], ["Cc1ccncc1"])
    cell = res["cells"][0]
    assert cell["status"] == "failed"                         # scaffold mismatch -> no trust
    assert cell["filled_reactants"][0] is None                # markush reactant: model derives it
    assert cell["filled_reactants"][1] == "ClCCl"             # constant reactant: kept verbatim


def test_enumerate_recovers_unsubstituted_parent_cell(monkeypatch):
    """The unsubstituted PARENT scope cell (every '*' = H) is now trusted: reverse_multi returns all-None on a
    MATCHED scaffold, fill-with-H roundtrips, and the parent reactant is recovered (was wrongly dropped before)."""
    import sys, pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "skills" / "chem_extract"))
    sys.path.insert(0, str(root / "skills" / "chem_extract_skills"))
    import engine.markush as MK
    monkeypatch.setattr(MK, "reverse_r", lambda *a, **k: {})  # dict-keyed reverse returns {} on the parent
    import tool as T
    # template product = N-substituted indole; concrete = the N-H parent (R = H) -> must be trusted, parent reactant filled
    res = T._enumerate_grid(["*n1ccc2ccccc12"], ["*n1cc(C)c2ccccc21"], ["Cc1c[nH]c2ccccc12"])
    cell = res["cells"][0]
    assert cell["status"] == "trusted"
    from rdkit import Chem
    assert Chem.MolToSmiles(Chem.MolFromSmiles(cell["filled_reactants"][0])) == Chem.MolToSmiles(Chem.MolFromSmiles("c1ccc2[nH]ccc2c1"))


def test_rdkit_reverse_fallback():
    """The RDKit reverse fallback (engine/markush.py) — used when MarkushMapMulti is unavailable (e.g. on the
    server, where reverse_r returns {}). Extracts R via RDKit ReplaceCore so markush_enumeration can populate."""
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "skills" / "chem_extract"))
    from engine.markush import _rdkit_reverse_r
    assert _rdkit_reverse_r("*c1ccccc1", "Cc1ccccc1") == {"R": "*C"}                 # R = methyl
    assert _rdkit_reverse_r("*C1CCc2ccccc2N1", "Cc1ccccc1C1CCc2ccccc2N1") == {"R": "*c1ccccc1C"}  # R = o-tolyl
    two = _rdkit_reverse_r("[*:1]c1ccc([*:2])cc1", "Cc1ccc(Cl)cc1")
    assert set(two.values()) == {"*C", "*Cl"}                                        # two R groups extracted


def test_fill_substitute_r_group(skill_run):
    # put an R group back into a markush template
    out, _ = skill_run("fill", "O=C(O)c1ccc(*)cc1<sep><a>13:R</a>", '{"R":"Cl"}')
    assert out == "O=C(O)c1ccc(Cl)cc1"


def test_cond_structuring(skill_run):
    out, _ = skill_run("cond", "Pd(OAc)2, MeCN, 120C; then NaBH4")
    d = json.loads(out)
    c = d["conditions"]
    assert len(c) == 2                              # split into two sub-steps on ';'/'then'
    roles = {x["role"] for x in c[0]}
    assert "solvent" in roles and "temperature" in roles
    assert c[1][0]["text"] == "NaBH4"


def test_abbrev_offline_table(skill_run):
    out, _ = skill_run("abbrev", "TES")
    d = json.loads(out)
    assert d["smiles"] == "*[Si](CC)(CC)CC"         # triethylsilyl fragment, from the local table


def test_canon_rejects_garbage(skill_run):
    out, p = skill_run("canon", "not_a_smiles_xyz")
    # invalid input must not crash the process; it yields empty/no canonical
    assert p.returncode == 0
    assert out == "" or "null" in out.lower() or out == "None"
