"""chem_validate - authoritative-library validation layer for the reaction extractor.

A small, self-contained toolbox another agent can import directly to turn extracted SMILES into
human text, resolve names to structures, dedup molecules, and sanity-check reactions. NONE of this
touches the extraction loop (no anti-leakage concern) - it is a POST-pass over already-extracted V2.

Backends (all verified working on the dev laptop 2026-06-07):
  - RDKit         : canonical SMILES, InChIKey (dedup key), validity, formula      [already a dep]
  - OPSIN         : name -> structure, OFFLINE + deterministic (needs Java)        [py2opsin]
  - PubChem       : name<->SMILES, SMILES->IUPAC/synonyms, existence check (HTTP)  [no key, no dep]
  - RXNMapper     : atom-atom mapping -> atom-conservation check                   [isolated venv]

Design rules honored:
  - PubChem is reachable DIRECTLY (NCBI/US). Do NOT route through a proxy (mirrors the MolParser
    rule), and there is no key to store.
  - OPSIN's OpenJDK is keg-only on this machine; OPSIN_JAVA_BIN / JAVA path is auto-discovered.
  - RXNMapper drags in torch; it lives in its OWN venv (RXNMAPPER_PY) so it never pollutes
    agent/.venv. Called out-of-process; degrades gracefully to None if absent.
  - Every backend fails soft: a network/dep outage returns None, never raises into the caller.

Quick start:
    from chem_validate import smiles_to_text, name_to_smiles, verify_smiles, inchikey, atom_conserved
    smiles_to_text("CC(=O)Oc1ccccc1C(=O)O")   # {'iupac':'2-acetyloxybenzoic acid','formula':...}
    name_to_smiles("4-bromoaniline")           # OPSIN first, PubChem fallback
    verify_smiles("CC(=O)Oc1ccccc1C(=O)O")     # {'status':'known','cid':2244}
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from functools import lru_cache
from typing import Optional

# --------------------------------------------------------------------------- config
PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
_PUBCHEM_MIN_INTERVAL = 0.22          # <= 5 req/s
_HTTP_TIMEOUT = 15
_last_call = [0.0]

# OpenJDK is keg-only here; let env override, else try the common brew path, else PATH.
_JAVA_CANDIDATES = [
    os.environ.get("OPSIN_JAVA_BIN", ""),
    "/opt/homebrew/opt/openjdk/bin/java",
    "/usr/local/opt/openjdk/bin/java",
    shutil.which("java") or "",
]
# python of the isolated rxnmapper venv (set via env to relocate)
RXNMAPPER_PY = os.environ.get("RXNMAPPER_PY", "/tmp/rxnmapper_venv/bin/python")


def _java_bin() -> Optional[str]:
    for c in _JAVA_CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


# Put Java on PATH at import so py2opsin's import-time `java -version` probe is quiet and OPSIN
# "just works" without per-call PATH juggling.
_JB = _java_bin()
if _JB:
    _jdir = os.path.dirname(_JB)
    if _jdir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _jdir + os.pathsep + os.environ.get("PATH", "")


# --------------------------------------------------------------------------- RDKit
@lru_cache(maxsize=4096)
def canonical(smiles: str, stereo: bool = True) -> Optional[str]:
    """RDKit canonical SMILES (None if unparseable). Markush/`*` allowed through RDKit."""
    try:
        from rdkit import Chem
        m = Chem.MolFromSmiles(smiles)
        if m is None:
            return None
        return Chem.MolToSmiles(m, isomericSmiles=stereo)
    except Exception:
        return None


@lru_cache(maxsize=4096)
def inchikey(smiles: str) -> Optional[str]:
    """InChIKey - the IUPAC-standard identifier; the robust dedup/cross-ref key (beats SMILES eq)."""
    try:
        from rdkit import Chem
        m = Chem.MolFromSmiles(smiles)
        return Chem.MolToInchiKey(m) if m is not None else None
    except Exception:
        return None


def formula(smiles: str) -> Optional[str]:
    try:
        from rdkit import Chem
        from rdkit.Chem import rdMolDescriptors
        m = Chem.MolFromSmiles(smiles)
        return rdMolDescriptors.CalcMolFormula(m) if m is not None else None
    except Exception:
        return None


def is_valid(smiles: str) -> bool:
    """Parses in RDKit AND has no wildcard atoms (a bare `*` = an unresolved R-group, not a final mol)."""
    try:
        from rdkit import Chem
        m = Chem.MolFromSmiles(smiles)
        return bool(m) and not any(a.GetAtomicNum() == 0 for a in m.GetAtoms())
    except Exception:
        return False


# --------------------------------------------------------------------------- OPSIN (offline)
@lru_cache(maxsize=4096)
def opsin_name_to_smiles(name: str, stereo: bool = True) -> Optional[str]:
    """Deterministic OFFLINE name->SMILES via OPSIN. Best for systematic IUPAC names. None on miss."""
    jb = _java_bin()
    if not jb or not name:
        return None
    try:
        from py2opsin import py2opsin     # shells out to `java`; PATH set at module import
    except Exception:
        return None
    try:
        out = py2opsin(name, output_format="SMILES")
    except Exception:
        out = ""
    out = (out or "").strip()
    if not out:
        return None
    return canonical(out, stereo=stereo) or out


def opsin_available() -> bool:
    return _java_bin() is not None


# --------------------------------------------------------------------------- PubChem (HTTP, no key)
def _pubchem(path: str, post: Optional[dict] = None) -> object:
    """One throttled (<=5 req/s), proxy-free PubChem call. GET, or POST when `post` form given.
    Returns parsed JSON / stripped text on 200; raises urllib.error.HTTPError on 4xx/5xx so callers
    can distinguish 404 (not found) from 400 (malformed). Throttle covers BOTH GET and POST."""
    dt = time.time() - _last_call[0]
    if dt < _PUBCHEM_MIN_INTERVAL:
        time.sleep(_PUBCHEM_MIN_INTERVAL - dt)
    url = f"{PUBCHEM}/{path}"
    # explicit empty-proxy opener: never inherit PROXY_* (NCBI is direct, US).
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = urllib.parse.urlencode(post).encode() if post else None
    for attempt in range(2):
        try:
            with opener.open(urllib.request.Request(url, data=data), timeout=_HTTP_TIMEOUT) as r:
                body = r.read().decode("utf-8", "ignore")
            _last_call[0] = time.time()
            return json.loads(body) if path.endswith("JSON") else body.strip()
        except urllib.error.HTTPError as e:
            _last_call[0] = time.time()
            if e.code == 503 and attempt == 0:        # rate limited -> back off once, retry
                time.sleep(1.0)
                continue
            raise                                      # 404 / 400 -> caller decides
    raise urllib.error.HTTPError(url, 503, "rate-limited", {}, None)  # type: ignore


def _pubchem_get(path: str, post: Optional[dict] = None) -> Optional[object]:
    """Soft wrapper: returns None instead of raising (for callers that don't care why it failed)."""
    try:
        return _pubchem(path, post=post)
    except Exception:
        return None


@lru_cache(maxsize=4096)
def pubchem_name_to_smiles(name: str) -> Optional[dict]:
    """{'smiles'(stereo),'connectivity_smiles','iupac','cid'} or None. Good for trivial/trade names."""
    if not name:
        return None
    q = urllib.parse.quote(name, safe="")
    data = _pubchem_get(f"compound/name/{q}/property/ConnectivitySMILES,SMILES,IUPACName/JSON")
    try:
        p = data["PropertyTable"]["Properties"][0]
        return {"smiles": p.get("SMILES"), "connectivity_smiles": p.get("ConnectivitySMILES"),
                "iupac": p.get("IUPACName"), "cid": p.get("CID")}
    except Exception:
        return None


@lru_cache(maxsize=4096)
def smiles_to_text(smiles: str, with_synonyms: bool = True, n_syn: int = 6) -> Optional[dict]:
    """SMILES -> human text for LLM-judging. {'iupac','formula','synonyms':[...],'cid'} or None.
    POSTs the SMILES (handles #, +, /, \\ that would break a GET path)."""
    if not smiles:
        return None
    out: dict = {"formula": formula(smiles)}
    d = _pubchem_get("compound/smiles/property/IUPACName/JSON", post={"smiles": smiles})
    try:
        p = d["PropertyTable"]["Properties"][0]       # type: ignore[index]
        out["iupac"] = p.get("IUPACName")
        out["cid"] = p.get("CID")
    except Exception:
        out["iupac"] = None
        out["cid"] = None
    if with_synonyms and out.get("cid"):
        syn = _pubchem_get(f"compound/cid/{out['cid']}/synonyms/JSON")
        try:
            out["synonyms"] = syn["InformationList"]["Information"][0]["Synonym"][:n_syn]
        except Exception:
            out["synonyms"] = []
    return out if (out.get("iupac") or out.get("formula")) else None


def verify_smiles(smiles: str) -> dict:
    """Existence check = OCSR sanity signal. status in {known, unknown, malformed}.
       known     -> real, indexed compound (high confidence the read is at least standardizable)
       unknown   -> valid structure, not in PubChem (common for novel products; weak signal)
       malformed -> PubChem can't standardize it (strong signal of an OCSR misread)."""
    if not smiles:
        return {"status": "malformed", "cid": None}
    try:
        txt = _pubchem("compound/smiles/cids/TXT", post={"smiles": smiles})
        cid = str(txt).strip().splitlines()[0] if txt else ""
        return {"status": "known", "cid": int(cid)} if cid and cid != "0" else {"status": "unknown", "cid": None}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "unknown", "cid": None}     # valid but not indexed
        return {"status": "malformed", "cid": None}        # 400 etc -> garbage read
    except Exception:
        return {"status": "unknown", "cid": None}          # network: don't punish the structure


# --------------------------------------------------------------------------- name resolution (combined)
def name_to_smiles(name: str, stereo: bool = True) -> Optional[dict]:
    """OPSIN first (offline, systematic names), PubChem fallback (trivial/trade names).
    Returns {'smiles','source','iupac'?,'cid'?} or None. Use to backfill text-reaction structures."""
    smi = opsin_name_to_smiles(name, stereo=stereo)
    if smi:
        return {"smiles": smi, "source": "opsin"}
    pc = pubchem_name_to_smiles(name)
    if pc and (pc.get("smiles") or pc.get("connectivity_smiles")):
        s = pc["smiles"] if stereo and pc.get("smiles") else (pc.get("connectivity_smiles") or pc.get("smiles"))
        return {"smiles": canonical(s, stereo=stereo) or s, "source": "pubchem",
                "iupac": pc.get("iupac"), "cid": pc.get("cid")}
    return None


# --------------------------------------------------------------------------- RXNMapper (isolated venv)
def rxnmapper_available() -> bool:
    return os.path.exists(RXNMAPPER_PY)


def atom_map(rxn_smiles: str) -> Optional[dict]:
    """Atom-atom map a 'reactants>>products' SMILES via RXNMapper (out-of-process).
    Returns {'mapped_rxn','confidence'} or None. rxn_smiles must be valid reaction SMILES."""
    if not rxnmapper_available() or ">>" not in rxn_smiles:
        return None
    code = (
        "import sys,json\n"
        "from rxnmapper import RXNMapper\n"
        "rxn=sys.argv[1]\n"
        "r=RXNMapper().get_attention_guided_atom_maps([rxn])[0]\n"
        "print(json.dumps({'mapped_rxn':r['mapped_rxn'],'confidence':r['confidence']}))\n"
    )
    try:
        out = subprocess.run([RXNMAPPER_PY, "-c", code, rxn_smiles],
                             capture_output=True, text=True, timeout=120)
        return json.loads(out.stdout.strip().splitlines()[-1]) if out.returncode == 0 else None
    except Exception:
        return None


def atom_conserved(reactant_smiles, product_smiles) -> Optional[dict]:
    """Heuristic conservation check WITHOUT RXNMapper: are all product heavy atoms (by element
    multiset) covered by the union of reactant atoms? A product needing atoms absent from every
    reactant => extraction is incomplete (missing a reagent) or the product was misread.
    Returns {'ok':bool,'missing':{element:count}} or None if anything won't parse."""
    from collections import Counter
    try:
        from rdkit import Chem
    except Exception:
        return None

    def atoms(smis):
        c = Counter()
        for s in (smis if isinstance(smis, (list, tuple)) else [smis]):
            m = Chem.MolFromSmiles(s) if s else None
            if m is None:
                return None
            for a in m.GetAtoms():
                if a.GetAtomicNum() > 1:
                    c[a.GetSymbol()] += 1
        return c

    ra, pa = atoms(reactant_smiles), atoms(product_smiles)
    if ra is None or pa is None:
        return None
    missing = {el: n - ra.get(el, 0) for el, n in pa.items() if n > ra.get(el, 0)}
    return {"ok": not missing, "missing": missing}


# --------------------------------------------------------------------------- self-test
if __name__ == "__main__":
    asp = "CC(=O)Oc1ccccc1C(=O)O"
    print("backends:", {"opsin": opsin_available(), "rxnmapper": rxnmapper_available()})
    print("canonical:", canonical(asp), "| inchikey:", inchikey(asp), "| formula:", formula(asp))
    print("opsin name->smiles:", opsin_name_to_smiles("4-bromoaniline"))
    print("name_to_smiles(celecoxib):", name_to_smiles("celecoxib"))
    print("smiles_to_text:", smiles_to_text(asp))
    print("verify(asp):", verify_smiles(asp))
    print("verify(garbage):", verify_smiles("C1CC2CC1C3CC2C4CCCCC4N5CCCCC5O123"))
    print("atom_conserved:", atom_conserved(["c1ccccc1Br", "Nc1ccccc1"], "c1ccccc1Nc1ccccc1"))
    am = atom_map("CC(=O)O.OCC>>CC(=O)OCC.O")
    print("rxnmapper:", am if am else "(rxnmapper venv not ready - optional)")
