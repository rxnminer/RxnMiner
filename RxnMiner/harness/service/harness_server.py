#!/usr/bin/env python3
"""harness_server — local product front-door for the skill-driven `chem_harness` agent.

Upload ONE figure image -> run our SkillOpt skill agent (NOT the chempipeline) -> register
the result as a chem_reviewer task -> deep-link the user into chem_reviewer to view/correct/export.

Flow per job (background thread):
  1. save upload as work/0.png  (jpg/etc -> png via PIL)
  2. dets = agent.tools.molparser("work/0.png")        # LIVE OCSR (closes the no-detections gap)
     write work/0_molparser.json                        # exactly what read_detections consumes
  3. chem_harness.run(paper_dir="work", figure_idx=0, out_path=result.json, caps)
  4. register as chem_reviewer task under PDF_ROOT/upload_<id>/
  5. reviewer_url = <REVIEWER_BASE>/#/upload_<id>/0

Run (needs the harness venv which has vertexai+rdkit):
  agent/.venv/bin/python service/harness_server.py        # -> http://localhost:8785
Also start the shared top-level reviewer:
  PDF_ROOT=/path/to/reviewer-data python ../reviewer/chem_reviewer/server.py --port 8767
"""
from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import io
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
from typing import Optional
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

try:  # POSIX product builds use an inter-process advisory lock.
    import fcntl
except ImportError:  # pragma: no cover - Windows falls back to the process lock.
    fcntl = None

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent"))
os.environ.setdefault("PYTHONPATH", str(REPO))

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

# ─────────────────────────────── config ───────────────────────────────
PORT = int(os.environ.get("PORT", "8785"))
REVIEWER_BASE = os.environ.get("REVIEWER_BASE", "http://localhost:8767")
PI_RUNTIME_BASE = os.environ.get("CHEMRXN_PI_RUNTIME_BASE", "http://127.0.0.1:4310").rstrip("/")
PI_RUNTIME_INTERNAL_TOKEN = os.environ.get("CHEMRXN_PI_INTERNAL_TOKEN", "")
LOCAL_RUNTIME_USER_ID = os.environ.get("CHEMRXN_LOCAL_RUNTIME_USER_ID", "")

def _reviewer_root() -> Path:
    """Resolve the shared reviewer package in the monorepo or a standalone harness export.

    `REVIEWER_ROOT` is canonical. `VIEWER_ROOT` remains a one-release compatibility override for
    existing deployments; callers should migrate without changing browser routes or runtime data.
    """
    override = os.environ.get("REVIEWER_ROOT") or os.environ.get("VIEWER_ROOT")
    if override:
        return Path(override)
    candidates = [
        REPO.parent / "reviewer",       # monorepo: <root>/harness + <root>/reviewer
        REPO / "reviewer",              # standalone export with reviewer copied beside service/
        REPO / "viewer",                # legacy standalone package
        REPO / "chem_viewer",           # legacy development checkout
    ]
    for candidate in candidates:
        if (candidate / "chem_reviewer").is_dir():
            return candidate
    return candidates[0]


REVIEWER_ROOT = _reviewer_root()
VIEWER_ROOT = REVIEWER_ROOT  # deprecated internal alias; remove after downstream deployments migrate
PDF_ROOT = Path(os.environ.get("PDF_ROOT") or (REVIEWER_ROOT / "PDF"))


def _git(*args) -> str:
    import subprocess
    try:
        return subprocess.check_output(["git", *args], cwd=str(REPO),
                                       stderr=subprocess.DEVNULL, timeout=3).decode().strip()
    except Exception:
        return ""


def _env_info() -> dict:
    """Which deployment is this? So a glance at the UI tells local vs server-stable vs server-dev apart (they share
    the same HTML). CR_ENV overrides; otherwise inferred from the repo path. git HEAD = the committed baseline
    (note: hot files may be scp'd ON TOP of HEAD), and `started` is when THIS process booted (post-deploy freshness)."""
    import socket
    label = os.environ.get("CR_ENV", "")
    if not label:
        p = str(REPO)
        label = ("local" if "/Users/" in p or "/home/" in p and "uniparser" not in p
                 else "server-dev" if "chemreaction-dev" in p
                 else "server-stable")
    return {"env": label, "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "commit": _git("rev-parse", "--short", "HEAD"), "host": socket.gethostname(),
            "started": time.time(), "port": PORT}


ENV_INFO = _env_info()                                  # computed once at boot
JOBS_DIR = Path(os.environ.get("JOBS_DIR") or (REPO / "product" / "harness_jobs"))  # env override for a dedicated instance (e.g. skill-loop viewer on an alt port)
DISTRIBUTED_RUNS_DIR = Path(os.environ.get("CHEMRXN_DISTRIBUTED_RUNS_DIR") or (JOBS_DIR.parent / "distributed-runs"))
DISTRIBUTED_CODE_ROOT = Path(os.environ.get("CHEMRXN_DISTRIBUTED_CODE_ROOT") or REPO.parent)
_VIEWER_DATA = os.environ.get("CHEMRXN_VIEWER_DATA")
DISTRIBUTED_PI_AGENT_DIR = Path(
    os.environ.get("CHEMRXN_DISTRIBUTED_PI_AGENT_DIR")
    or ((Path(_VIEWER_DATA) / "pi-auth") if _VIEWER_DATA else (JOBS_DIR.parent / "pi-auth"))
)
DISTRIBUTED_PROCESSES: dict[str, subprocess.Popen] = {}
# Optional colon-separated roots let a clean/deployed UI associate staged benchmark images with the
# read-only reviewed corpus without copying GT into the deployment or prediction directory.
_GT_ROOTS_ENV = os.environ.get("GT_ROOTS", "")
GT_ROOTS = ([Path(p) for p in _GT_ROOTS_ENV.split(os.pathsep) if p] if _GT_ROOTS_ENV else
            [REPO / "pdf_benchmark", REPO / "pdf_benchmark_revised" / "修正",
             REPO / "pdf_benchmark_revised"])
CAP_ITERS = int(os.environ.get("CAP_ITERS", "200"))            # 40 -> 200: dense scope grids (20-33 variants) ran out
CAP_TOKENS = int(os.environ.get("CAP_TOKENS", "9000000"))      # 900k -> 9M (~$0.3 -> ~$3/fig): completeness > token cost
JOB_DEADLINE_S = int(os.environ.get("JOB_DEADLINE_S", "2700")) # 900 -> 2700s: the bigger token budget needs wall-clock to spend
# gemini-3.1-pro-preview price (USD per 1M tokens). Vertex bills a HIGHER tier on any single request whose
# INPUT exceeds 200k tokens: <=200k -> $2 in / $12 out; >200k -> $4 in / $18 out. The tier is PER-REQUEST,
# not cumulative -- our agent makes many small requests (max ~46k each on the biggest job), so they bill at
# the low tier; the high tier only bites a single huge-context request (e.g. HP mode). Override to your rate.
PRICE_IN = float(os.environ.get("GEMINI_PRICE_IN_PER_M", "2.0"))
PRICE_OUT = float(os.environ.get("GEMINI_PRICE_OUT_PER_M", "12.0"))
PRICE_IN_HI = float(os.environ.get("GEMINI_PRICE_IN_PER_M_HI", "4.0"))     # request input > 200k tokens
PRICE_OUT_HI = float(os.environ.get("GEMINI_PRICE_OUT_PER_M_HI", "18.0"))
TIER_THRESHOLD = int(os.environ.get("GEMINI_TIER_THRESHOLD", "200000"))
CACHE_FACTOR = float(os.environ.get("GEMINI_CACHE_FACTOR", "0.25"))   # cached prefix billed at ~1/4 input
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _cost(tin, tout, tcached=0):
    """Flat low-tier estimate (used for the LIVE running figure where we only have cumulative totals)."""
    tin, tout, tcached = tin or 0, tout or 0, tcached or 0
    fresh = max(0, tin - tcached)
    return round((fresh + tcached * CACHE_FACTOR) / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT, 4)


def _cost_tiered(transcript):
    """Accurate FINAL cost from per-request token deltas: each request bills at the >200k tier if ITS OWN input
    exceeds the threshold. transcript steps carry CUMULATIVE tok_in/out/cached, so the per-request amount is the
    delta between consecutive steps. Falls back to 0 if the transcript has no token data."""
    steps = [s for s in (transcript or []) if s.get("tok_in") is not None]
    pin = pout = pcached = 0.0
    total = 0.0
    for s in steps:
        ti, to, tc = s.get("tok_in") or 0, s.get("tok_out") or 0, s.get("tok_cached") or 0
        rin, rout, rcached = max(0, ti - pin), max(0, to - pout), max(0, tc - pcached)
        pin, pout, pcached = ti, to, tc
        hi = rin > TIER_THRESHOLD
        p_in, p_out = (PRICE_IN_HI, PRICE_OUT_HI) if hi else (PRICE_IN, PRICE_OUT)
        fresh = max(0, rin - rcached)
        total += (fresh + rcached * CACHE_FACTOR) / 1e6 * p_in + rout / 1e6 * p_out
    return round(total, 4)


def _placeholder_svg(text: str, w: int, h: int) -> str:
    esc = (text or "(empty)").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:48]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
            f'<rect width="100%" height="100%" fill="#f4f5f7" rx="6"/>'
            f'<text x="50%" y="50%" fill="#9aa0ab" font-size="11" text-anchor="middle" '
            f'dominant-baseline="middle" font-family="monospace">{esc}</text></svg>')


def _render_svg(smi: str, w: int, h: int) -> str:
    """SMILES -> RDKit SVG (white bg, for the molecule/reaction tabs). Markush '*' renders as a
    dummy atom; invalid/empty -> a labeled placeholder. Never raises."""
    s = (smi or "").split("<sep>")[0].strip()
    if not s:
        return _placeholder_svg("(no structure)", w, h)
    try:
        from rdkit import Chem, RDLogger
        from rdkit.Chem.Draw import rdMolDraw2D
        RDLogger.DisableLog("rdApp.*")
        m = Chem.MolFromSmiles(s)
        if m is None:
            return _placeholder_svg(s, w, h)
        d = rdMolDraw2D.MolDraw2DSVG(w, h)
        rdMolDraw2D.PrepareAndDrawMolecule(d, m)
        d.FinishDrawing()
        return d.GetDrawingText()
    except Exception:
        return _placeholder_svg(s, w, h)


def _read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _canon(smi: str):
    s = (smi or "").split("<sep>")[0].strip()
    if not s:
        return None
    try:
        from rdkit import Chem, RDLogger
        RDLogger.DisableLog("rdApp.*")
        m = Chem.MolFromSmiles(s)
        return Chem.MolToSmiles(m) if m else None
    except Exception:
        return None


_GT_INDEX = None
def _build_gt_index() -> dict:
    """md5(original benchmark figure png) -> its GT reviewed.json, so an uploaded benchmark figure
    auto-associates with ground truth (matched on the RAW upload bytes, before PIL re-encode).
    Scans pdf_benchmark AND the re-proofread pdf_benchmark_revised; revised is scanned LAST so for a
    figure present in both, the corrected GT wins the (same) md5 key."""
    import hashlib
    idx = {}
    for base in GT_ROOTS:
        if not base.exists():
            continue
        # PDF releases may add one version directory above the paper directory
        # (pdf_benchmark_revised/<release>/<paper>/<figure>.png). Recursive discovery keeps
        # uploaded/re-registered paper anchors connected to the reviewer GT by image MD5.
        for png in base.glob("**/[0-9]*.png"):
            stem = png.stem
            for cand in (png.parent / f"{stem}_output.reviewed.json",
                         png.parent / f"{stem}_predicted_output.reviewed.json"):
                if cand.exists():
                    try:
                        idx[hashlib.md5(png.read_bytes()).hexdigest()] = str(cand)
                    except Exception:
                        pass
                    break
    # GT1 (ChemEAGLE) / GT2 (OpenChemIE) registry: each subdir has one figure image (any extension, original
    # bytes) + gt.reviewed.json (converted to V2 by scripts/build_gt_registry.py). Lets the vs-GT tab light up
    # for external-benchmark figures too, not just our pdf_benchmark.
    reg = REPO / "relatedworks" / "gt_v2"
    if reg.exists():
        for sub in reg.iterdir():
            gt = sub / "gt.reviewed.json"
            if not (sub.is_dir() and gt.exists()):
                continue
            img = next((p for p in sub.iterdir()
                        if p.suffix.lower() in (".png", ".jpg", ".jpeg") and p.name != "gt.reviewed.json"), None)
            if img:
                try:
                    idx[hashlib.md5(img.read_bytes()).hexdigest()] = str(gt)
                except Exception:
                    pass
    return idx


def _gt_for_upload(input_file: Path):
    global _GT_INDEX
    if _GT_INDEX is None:
        _GT_INDEX = _build_gt_index()
    import hashlib
    try:
        return _GT_INDEX.get(hashlib.md5(input_file.read_bytes()).hexdigest())
    except Exception:
        return None


def _v2_mols(v2: dict, side=None):
    """Canonical (markush-stripped, '*'-free) SMILES set from a V2 (reactants/products pooled)."""
    from tools_product.score_paper import norm_mol
    out = set()
    for grp in (v2.get("selected_examples") or []) + (v2.get("reaction_templates") or []):
        for sd in ([side] if side else ["reactants", "products"]):
            for m in (grp.get(sd) or []):
                c = norm_mol(m.get("E-SMILES") or m.get("smiles") or "", False)
                if c and "*" not in c:
                    out.add(c)
    return out


def _det_mols(dets: list):
    from tools_product.score_paper import norm_mol
    out = set()
    for d in dets or []:
        if isinstance(d, dict):
            c = norm_mol(d.get("smi") or "", False)
            if c and "*" not in c:
                out.add(c)
    return out


def _cond_set(v2: dict):
    """All condition texts (normalized) pooled from examples + templates. Role-agnostic so it
    survives the schema differences; lets us measure condition coverage vs GT."""
    s = set()
    for grp in (v2.get("selected_examples") or []) + (v2.get("reaction_templates") or []):
        for step in (grp.get("conditions") or []):
            for c in (step or []):
                if isinstance(c, dict):
                    t = (c.get("text") or "").strip().lower()
                    if t:
                        s.add(t)
    return s


def _yields(v2: dict):
    """{product_id -> yield value} (normalized, '%'-stripped). Handles both our {type,content,
    for_product} and GT's {role,of,text} metric shapes."""
    out = {}
    for e in (v2.get("selected_examples") or []):
        for m in (e.get("metrics") or []):
            if not isinstance(m, dict):
                continue
            kind = (m.get("type") or m.get("role") or "").lower()
            if kind != "yield":
                continue
            pid = str(m.get("for_product") or m.get("of") or "").strip()
            val = str(m.get("content") or m.get("value") or m.get("text") or "").replace("%", "").strip()
            if pid and val:
                out[pid] = val
    return out


def _cmp(found: set, gt: set):
    inter = len(found & gt)
    rec = inter / len(gt) if gt else None
    prec = inter / len(found) if found else None
    f1 = (2 * rec * prec / (rec + prec)) if (rec and prec) else (0.0 if (gt and found) else None)
    return {"n": len(found), "correct": inter, "missing": len(gt) - inter,
            "recall": round(rec, 3) if rec is not None else None,
            "precision": round(prec, 3) if prec is not None else None,
            "f1": round(f1, 3) if f1 is not None else None}


def _mol_origin(src: dict, det: dict) -> str:
    src = src or {}
    if src.get("origin"):                                  # authoritative tag from the emit engine
        return src["origin"]
    if src.get("molparser_markush") or (det or {}).get("markush"):
        return "markush"
    if src.get("molparser_md5") or src.get("bbox_index") is not None or src.get("bbox") or det:
        return "molparser"
    return "agent"

# ─────────────────────────────── lightweight accounts ───────────────────────────────
# Demo-grade accounts: register/login -> a server-side session token in an httponly cookie. Passwords are
# pbkdf2-hashed + salted (never stored plaintext). Each job is tagged with its owner so a user sees ONLY their
# own uploads. Guests (not logged in) get a per-browser session id and are isolated the same way. This is the
# multi-tenancy precursor to per-user API keys (feature A). NOT a substitute for a real IdP in production.
AUTH_DIR = REPO / "product" / "auth"
USERS_FILE = AUTH_DIR / "users.json"
SESSIONS_FILE = AUTH_DIR / "sessions.json"
COOKIE = "cr_sid"
_PBKDF_ROUNDS = 200_000


def _auth_load(p: Path) -> dict:
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _auth_save(p: Path, data: dict) -> None:
    try:
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, p)
    except Exception:
        pass


USERS: dict = _auth_load(USERS_FILE)          # username -> {salt, hash, created}
SESSIONS: dict = _auth_load(SESSIONS_FILE)    # token -> {user|null, created}  (user=None => guest)


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PBKDF_ROUNDS).hex()


def _new_session(user: str | None) -> str:
    tok = secrets.token_urlsafe(32)
    SESSIONS[tok] = {"user": user, "created": time.time()}
    _auth_save(SESSIONS_FILE, SESSIONS)
    return tok


def _identity(request: Request) -> str:
    """Owner key for the caller: the logged-in username, or 'guest:<sid>' for an anonymous browser session.
    Returns '' only if there is no cookie at all (the caller endpoint then mints one)."""
    tok = request.cookies.get(COOKIE)
    if not tok:
        return ""
    sess = SESSIONS.get(tok)
    if not sess:
        return ""
    return sess["user"] if sess.get("user") else f"guest:{tok[:12]}"


def _ensure_session(request: Request, resp: Response) -> str:
    """Return the caller's identity, minting a guest session + cookie if they have none."""
    ident = _identity(request)
    if ident:
        return ident
    tok = _new_session(None)
    resp.set_cookie(COOKIE, tok, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return f"guest:{tok[:12]}"


# ── chemagent access-key gate for PAID models (managed Gemini / Azure / geek) ──
# An account is "privileged" if it is an admin (the demo account, or any user with role=='admin' in users.json)
# OR its session was unlocked with the chemagent access key (POST /api/unlock). public (local vLLM) + byo need none.
ADMIN_USERS = {"demo"}


def _is_privileged(request: Request) -> bool:
    if os.environ.get("ALL_PRIVILEGED") == "1":   # instance-wide: paid models usable without unlock
        return True
    tok = request.cookies.get(COOKIE) or ""
    sess = SESSIONS.get(tok) or {}
    if sess.get("privileged"):
        return True
    user = sess.get("user")
    if user and (user in ADMIN_USERS or (USERS.get(user) or {}).get("role") == "admin"):
        return True
    return False


def _chemagent_key():
    """The paid-unlock secret, read at runtime from .env (model_registry._env); never logged/echoed."""
    try:
        from model_registry import _env
        return _env("chemagent_access_key")
    except Exception:
        return None


# ─────────────────────────── BYO LLM key vault (Fernet, per-session, opt-in) ───────────────────────────
# The user's own LLM key is a SECRET. If they tick "remember this session", we store it ENCRYPTED (Fernet),
# keyed by a server-side secret combined with their cr_sid -- so the on-disk vault is useless without BOTH the
# server secret file and the session cookie. The plaintext key never touches disk; GET never returns it.
SECRET_FILE = AUTH_DIR / "secret.key"
VAULT_FILE = AUTH_DIR / "keyvault.json"      # cr_sid token -> {"provider", "model", "enc": <fernet ciphertext>}


def _server_secret() -> bytes:
    """A 32-byte server secret, generated once and persisted 0600. Mixed into every session's Fernet key."""
    try:
        if SECRET_FILE.exists():
            return SECRET_FILE.read_bytes()
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        sec = secrets.token_bytes(32)
        SECRET_FILE.write_bytes(sec)
        try:
            os.chmod(SECRET_FILE, 0o600)
        except Exception:
            pass
        return sec
    except Exception:
        # last-resort ephemeral secret (vault won't survive a restart, but never crashes the request path)
        return hashlib.sha256(b"chemreaction-ephemeral").digest()


def _session_fernet(sid: str):
    """A Fernet derived from sha256(server_secret + sid). Same sid+secret -> same key (deterministic)."""
    from cryptography.fernet import Fernet
    raw = hashlib.sha256(_server_secret() + sid.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def _vault_sid(request: Request) -> str:
    return request.cookies.get(COOKIE) or ""


def _vault_get_key(sid: str) -> str | None:
    """Decrypt the session's saved BYO key, or None if absent/undecryptable. Never raises."""
    if not sid:
        return None
    rec = _auth_load(VAULT_FILE).get(sid)
    if not rec or not rec.get("enc"):
        return None
    try:
        return _session_fernet(sid).decrypt(rec["enc"].encode()).decode()
    except Exception:
        return None


def _vault_get_meta(sid: str) -> dict:
    """Non-secret view of the saved key (does the session have one, and for which provider/model). Never the key."""
    rec = (_auth_load(VAULT_FILE).get(sid) or {}) if sid else {}
    return {"has_key": bool(rec.get("enc")), "provider": rec.get("provider"), "model": rec.get("model")}


def _vault_set(sid: str, provider: str, model: str, key: str) -> bool:
    if not sid or not key:
        return False
    try:
        enc = _session_fernet(sid).encrypt(key.encode()).decode()
    except Exception:
        return False
    v = _auth_load(VAULT_FILE)
    v[sid] = {"provider": provider, "model": model, "enc": enc}
    _auth_save(VAULT_FILE, v)
    return True


def _vault_clear(sid: str) -> None:
    if not sid:
        return
    v = _auth_load(VAULT_FILE)
    if v.pop(sid, None) is not None:
        _auth_save(VAULT_FILE, v)


app = FastAPI(title="ChemReaction — skill-agent front-door")
_pool = ThreadPoolExecutor(max_workers=2)              # Gemini rate-limit friendly
JOBS: dict[str, dict] = {}


@app.middleware("http")
async def protect_local_product_boundary(request: Request, call_next):
    """Block cross-site reads/writes and DNS rebinding at the local product boundary."""
    host_header = request.headers.get("host", "")
    try:
        host_name = urllib.parse.urlsplit(f"//{host_header}").hostname or ""
    except ValueError:
        host_name = ""
    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    if os.environ.get("PYTEST_CURRENT_TEST"):
        allowed_hosts.add("testserver")
    if host_name.lower() not in allowed_hosts:
        return JSONResponse(
            {"detail": "Local product request rejected: invalid Host"},
            status_code=403,
        )
    origin = request.headers.get("origin")
    if origin:
        parsed_origin = None
        try:
            parsed_origin = urllib.parse.urlsplit(origin)
            origin_host = (parsed_origin.hostname or "").lower()
            origin_netloc = parsed_origin.netloc.lower()
        except ValueError:
            origin_host = origin_netloc = ""
        if (
            parsed_origin is None
            or parsed_origin.scheme not in {"http", "https"}
            or origin_host not in allowed_hosts
            or origin_netloc != host_header.lower()
        ):
            return JSONResponse(
                {"detail": "Local product request rejected: cross-site Origin"},
                status_code=403,
            )
    elif request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        top_level_product_navigation = (
            request.method in {"GET", "HEAD"}
            and request.url.path in {"", "/", "/index.html"}
            and request.headers.get("sec-fetch-mode", "").lower() == "navigate"
            and request.headers.get("sec-fetch-dest", "").lower() == "document"
        )
        if not top_level_product_navigation:
            return JSONResponse(
                {"detail": "Local product request rejected: cross-site request"},
                status_code=403,
            )
    job_match = re.fullmatch(r"/api/jobs/([^/]+)(?:/.*)?", request.url.path)
    if job_match:
        job_id = urllib.parse.unquote(job_match.group(1))
        if (
            job_id
            and not job_id.startswith(".")
            and all(character.isalnum() or character in "._-" for character in job_id)
        ):
            meta = JOBS.get(job_id) or _read_json(JOBS_DIR / job_id / "meta.json") or {}
            recorded_owner = meta.get("owner")
            # Clone-first local mode can mix classic cookie-owned uploads with
            # runtime-owned workspaces in the same browser. Both authenticated
            # identities are valid in that explicitly single-user mode.
            allowed_owners = {
                value for value in (_identity(request), LOCAL_RUNTIME_USER_ID)
                if value
            }
            if recorded_owner and recorded_owner not in allowed_owners:
                return JSONResponse({"detail": "not your job"}, status_code=403)
    return await call_next(request)


# Serve the Ketcher editor bundle SAME-ORIGIN so the in-product molecule editor can reach
# iframe.contentWindow.ketcher (cross-origin to the :8767 reviewer would be blocked). Reuses the static bundle
# shipped with the shared reviewer. Existence-guarded: if the bundle is absent the mount is simply
# skipped and the edit modal degrades gracefully (its iframe 404s, save is disabled).
_KETCHER_DIR = Path(os.environ.get("KETCHER_DIR") or (REVIEWER_ROOT / "vendor" / "ketcher"))
if not _KETCHER_DIR.exists():
    _KETCHER_DIR = REVIEWER_ROOT / "ketcher"  # one-release compatibility with the old layout
if _KETCHER_DIR.exists():
    from fastapi.staticfiles import StaticFiles  # noqa: E402
    app.mount("/ketcher", StaticFiles(directory=str(_KETCHER_DIR), html=True), name="ketcher")

_REVIEWER_WEB_DIR = REVIEWER_ROOT / "chem_reviewer" / "web"
if _REVIEWER_WEB_DIR.exists():
    from fastapi.staticfiles import StaticFiles  # noqa: E402
    # Document Mode mounts a Vue ESM island from the shared top-level reviewer.
    # The figure reviewer already uses these exact modules under /static.
    app.mount("/static", StaticFiles(directory=str(_REVIEWER_WEB_DIR)), name="reviewer-static")


@app.api_route(
    "/api/reviewer/{proxy_path:path}",
    methods=["GET", "HEAD", "POST", "DELETE"],
)
async def api_reviewer_proxy(proxy_path: str, request: Request):
    """Same-origin bridge for the one shared Figure reviewer Vue island.

    The browser never talks to the reviewer's second loopback port directly. This keeps the
    product shell, CSRF boundary, and chemistry renderer in one origin without duplicating the
    reviewer's task/save APIs.
    """
    if not proxy_path.startswith("api/") or ".." in proxy_path.split("/"):
        raise HTTPException(404, "Reviewer route not found")
    paper_match = re.match(r"^api/paper/([^/]+)(?:/|$)", proxy_path)
    if not paper_match:
        # The product embeds one task-scoped Figure island. The upstream
        # reviewer also exposes collection-wide and GT-bug sidecar routes;
        # neither belongs to this product bridge, and forwarding them would
        # create an owner-isolation bypass.
        raise HTTPException(404, "Reviewer route is not exposed")
    slug = urllib.parse.unquote(paper_match.group(1))
    if (
        not slug.startswith("upload_")
        or len(slug) <= len("upload_")
        or not all(character.isalnum() or character in "._-" for character in slug)
    ):
        raise HTTPException(404, "Reviewer task not found")
    job_id = slug[len("upload_"):]
    meta = JOBS.get(job_id) or _read_json(JOBS_DIR / job_id / "meta.json") or {}
    if not meta:
        raise HTTPException(404, "Reviewer task not found")
    _require_job_owner(meta, request=request)
    target = f"{REVIEWER_BASE.rstrip('/')}/{proxy_path}"
    if request.url.query:
        target += f"?{request.url.query}"
    body = await request.body() if request.method in {"POST", "DELETE"} else None
    headers = {"Accept": request.headers.get("accept", "*/*")}
    if request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]
    upstream_request = urllib.request.Request(
        target,
        data=body,
        headers=headers,
        method=request.method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(upstream_request, timeout=30) as upstream:
            response_body = upstream.read()
            status = upstream.status
            media_type = upstream.headers.get_content_type()
            content_disposition = upstream.headers.get("Content-Disposition")
    except urllib.error.HTTPError as exc:
        response_body = exc.read() if exc.code < 500 else b'{"detail":"Reviewer request failed"}'
        status = exc.code
        media_type = exc.headers.get_content_type() if exc.headers else "application/json"
        content_disposition = None
        exc.close()
    except Exception:
        raise HTTPException(503, "Local chemistry reviewer not connected") from None
    response_headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if content_disposition:
        response_headers["Content-Disposition"] = content_disposition
    return Response(
        content=response_body if request.method != "HEAD" else b"",
        status_code=status,
        media_type=media_type,
        headers=response_headers,
    )


def _clean_username(u: str) -> str:
    return "".join(c for c in (u or "").strip() if c.isalnum() or c in "_-.")[:40]


@app.post("/api/register")
async def api_register(request: Request):
    body = await request.json()
    user = _clean_username(body.get("username", ""))
    pw = body.get("password") or ""
    if len(user) < 3:
        raise HTTPException(400, "username must be >=3 chars (letters, digits, _-.)")
    if len(pw) < 6:
        raise HTTPException(400, "password must be >=6 chars")
    if user in USERS:
        raise HTTPException(409, "username taken")
    salt = secrets.token_hex(16)
    USERS[user] = {"salt": salt, "hash": _hash_pw(pw, salt), "created": time.time()}
    _auth_save(USERS_FILE, USERS)
    resp = JSONResponse({"username": user})
    resp.set_cookie(COOKIE, _new_session(user), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = _clean_username(body.get("username", ""))
    pw = body.get("password") or ""
    rec = USERS.get(user)
    if not rec or not secrets.compare_digest(rec["hash"], _hash_pw(pw, rec["salt"])):
        raise HTTPException(401, "wrong username or password")
    resp = JSONResponse({"username": user})
    resp.set_cookie(COOKIE, _new_session(user), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/api/logout")
def api_logout(request: Request):
    tok = request.cookies.get(COOKIE)
    if tok and tok in SESSIONS:
        SESSIONS.pop(tok, None); _auth_save(SESSIONS_FILE, SESSIONS)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
def api_me(request: Request):
    tok = request.cookies.get(COOKIE)
    sess = SESSIONS.get(tok or "")
    return {"username": (sess.get("user") if sess else None), "authenticated": bool(sess and sess.get("user"))}


MAX_UPLOAD_BYTES = 25 * 1024 * 1024                    # 25 MB upload cap (single-process server -> OOM/DoS guard)
MAX_RUNTIME_FIGURE_BYTES = 50 * 1024 * 1024
MAX_RUNTIME_PDF_BYTES = 200 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000                          # ~50 MP decode cap (decompression-bomb guard)
MAX_BATCH = 20                                         # max images per batch upload (each becomes its own job)


# ─────────────────────────────── helpers ───────────────────────────────
def _to_png(src: Path, dst: Path) -> None:
    from PIL import Image
    # Guard decompression bombs: PIL raises DecompressionBombError above this many pixels (attacker-controlled
    # bytes are decoded here on the single-process server). Surfaces as a normal job error, not an OOM crash.
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    im = Image.open(src)
    if im.mode not in ("RGB", "RGBA", "L", "P"):
        im = im.convert("RGB")
    im.save(dst, "PNG")


def _normalize_v2(data: dict, slug: str, tid: str) -> dict:
    """Backfill the manifest fields chem_reviewer needs, without touching chemistry.
    Mirrors chem_viewer/import_ours.py:normalize_prediction."""
    data.setdefault("task_id", f"{slug}_t{tid}")
    data.setdefault("paper_id", slug)
    data["anchor_image"] = f"{tid}.png"
    if not data.get("anchor_figure") or data.get("anchor_figure") in ("0.png", "1.png", "2.png"):
        data["anchor_figure"] = "uploaded figure"
    data.setdefault("anchor_page", 1)
    data.setdefault("extractor", "chem_harness skill agent (gemini-3.1-pro-preview)")
    data.setdefault("is_multistep_synthesis", bool(data.get("reaction_graph")))
    return data


def _write_reactions_json(out_dir: Path, slug: str, title: str) -> None:
    out_dir.joinpath("reactions.json").write_text(
        json.dumps({"paper_id": slug, "title": title, "page_count": 1,
                    "extractor": "chem_harness skill agent (gemini-3.1-pro-preview)"},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _classify_incomplete(tr: list) -> str:
    tail = tr[-1] if tr else {}
    if "stop" in tail:
        return "token_cap"
    if "error" in tail:
        return "model_error"
    crops = sum(1 for s in tr if s.get("call") == "crop")
    emits = sum(1 for s in tr if s.get("call") == "emit_full")
    if crops >= 6 and emits == 0:
        return "crop_loop_no_emit"
    return "incomplete_no_emit"


def _save_meta(job_id: str) -> None:
    """Persist a small job record to disk so the history sidebar survives restarts."""
    job = JOBS.get(job_id) or {}
    try:
        (JOBS_DIR / job_id).mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            k: job.get(k) for k in ("id", "filename", "mode", "status", "phase",
                                    "n_examples", "n_pages", "n_anchors", "n_reactions", "owner",
                                    "n_detections", "created", "valid", "reason", "cost_usd",
                                    # persist token/iter so a reopened or post-restart job still shows them
                                    # (the page reads these from meta when the job is no longer in memory)
                                    "tokens", "tokens_in", "tokens_out", "tokens_cached", "iters",
                                    "elapsed_s", "started", "ended", "queue_wait_s", "reviewer_url", "note",
                                    # which model ran (non-secret); the BYO api_key is NEVER persisted here
                                    "provider", "model_name", "tag",
                                    # Source-rights metadata is public provenance, never a credential.
                                    "license", "attribution", "source_doi", "source_url",
                                    "license_url", "change_notice")})
        # Atomic write: a concurrent history scan (every 5s/client) must never read a half-written meta.json
        # (a torn read -> json parse fail -> the job silently vanishes from that listing). tmp+rename is atomic.
        meta = JOBS_DIR / job_id / "meta.json"
        tmp = meta.with_suffix(".json.tmp")
        tmp.write_text(payload)
        os.replace(tmp, meta)
    except Exception:
        pass


def _ensure_ketcher() -> None:
    """Create only the external runtime data root; source/vendor paths are immutable."""
    PDF_ROOT.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────── worker ───────────────────────────────
def _worker(job_id: str, input_path: Path, title: str, mode: str = "fast",
            provider: str = "gemini-vertex", api_key: str = None, model_name: str = None,
            conn: dict = None) -> None:
    # api_key (the user's BYO LLM key) is a LOCAL param only -- never written to JOBS[job_id], meta.json, or logs.
    job = JOBS[job_id]
    t0 = time.time()
    job["started"] = t0                                     # worker pickup (after queue wait); created = enqueue time
    try:
        import chem_harness as H            # agent/ on path; has its own Vertex creds
        from tools import molparser

        jdir = JOBS_DIR / job_id
        work = jdir / "work"
        work.mkdir(parents=True, exist_ok=True)
        png = work / "0.png"
        job.update(status="running", phase="prep")
        _to_png(input_path, png)

        # 1) LIVE detection — closes the "uploaded image has no detections" gap.
        # EXPERIMENT no_molparser: skip OCSR entirely -> no 0_molparser.json -> H.run + read_detections + bbox
        # enrich all degrade to pure model vision (the single chokepoint for the "no interference" ablation).
        job["phase"] = "molparser"
        if (conn or {}).get("no_molparser"):
            dets = []
        else:
            try:
                dets = molparser(str(png))
            except Exception as e:
                job.update(status="error", phase="molparser",
                           error=f"OCSR service unreachable: {type(e).__name__}: {e}")
                return
            (work / "0_molparser.json").write_text(json.dumps(dets))
        job["n_detections"] = len(dets)
        try:                                  # labeled overview (boxes + index numbers) for model + UI
            H.annotate_detections(str(png), dets, str(work / "0_annotated.png"))
        except Exception:
            pass

        # 2) the skill agent (stream tool steps + live token accounting via on_step)
        job["phase"] = "agent"
        job["transcript"] = []

        def _on_step(s):
            job["transcript"].append(dict(s))
            if s.get("tok_in") is not None:               # live input/output/cached token split + price
                job["tokens_in"], job["tokens_out"] = s["tok_in"], s.get("tok_out")
                job["tokens_cached"] = s.get("tok_cached")
                job["tokens"] = (s["tok_in"] or 0) + (s.get("tok_out") or 0)
                job["cost_usd"] = _cost(s["tok_in"], s.get("tok_out"), s.get("tok_cached"))

        out_path = str(jdir / "result.json")
        # Wall-clock deadline: the agent checks should_stop at every loop top, so a runaway (looping) job
        # self-terminates instead of pinning 1 of 2 worker slots forever. (A single hung network call still
        # needs the Vertex-level request timeout; this covers the common runaway-iteration case.)
        _deadline = time.time() + JOB_DEADLINE_S
        _conn = conn or {}
        res = H.run(str(work), 0, out_path, max_iters=CAP_ITERS, max_tokens=CAP_TOKENS, on_step=_on_step,
                    should_stop=lambda: bool(JOBS.get(job_id, {}).get("cancel")) or time.time() > _deadline,
                    mode=mode, provider=provider, api_key=api_key, model_name=model_name,
                    base_url=_conn.get("base_url"), azure_endpoint=_conn.get("azure_endpoint"),
                    api_version=_conn.get("api_version"),
                    use_detections=not _conn.get("no_molparser"), grid_big=bool(_conn.get("grid_big")),
                    understand_first=bool(_conn.get("understand_first")),
                    images_mode=_conn.get("images_mode") or "full")
        timed_out = time.time() > _deadline and not any(s.get("call") == "finish" for s in res.get("transcript", []))
        tr = res.get("transcript", [])
        try:                                          # persist so reopening a past job still shows the trace
            (jdir / "transcript.json").write_text(json.dumps(tr))
        except Exception:
            pass
        # FINAL cost: tier-aware per-request (correctly bills any >200k-input request at the high tier),
        # falling back to the flat estimate if the transcript lacks per-step token data.
        _final_cost = _cost_tiered(tr) or _cost(res.get("tokens_in"), res.get("tokens_out"), res.get("tokens_cached"))
        job.update(transcript=tr, tokens=res.get("tokens"), tokens_in=res.get("tokens_in"),
                   tokens_out=res.get("tokens_out"), tokens_cached=res.get("tokens_cached"),
                   iters=res.get("iters"), cost_usd=_final_cost)

        # 3) register as a chem_reviewer task
        job["phase"] = "registering"
        slug, tid = f"upload_{job_id}", "0"
        out_dir = PDF_ROOT / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(png, out_dir / f"{tid}.png")
        _write_reactions_json(out_dir, slug, title)

        finished = any(s.get("call") == "finish" for s in tr)
        if Path(out_path).exists():
            data = _normalize_v2(json.loads(Path(out_path).read_text()), slug, tid)
            (out_dir / f"{tid}_predicted_output.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            job["reviewer_url"] = f"{REVIEWER_BASE}/#/{slug}/{tid}"
            job["n_examples"] = len(data.get("selected_examples") or [])
            if job.get("cancel"):                         # user pressed Stop -> keep partial draft, flag it
                job.update(status="incomplete", phase="done", valid=False, reason="user-stopped")
            elif bool(res.get("valid")) and finished:
                job.update(status="done", phase="done", valid=True)
            else:
                job.update(status="incomplete", phase="done", valid=False,
                           reason=("timeout" if timed_out else _classify_incomplete(tr)))
        else:
            job.update(status="incomplete", phase="done", valid=False,
                       reason=("user-stopped" if job.get("cancel") else _classify_incomplete(tr)),
                       note="agent produced no V2 (no emit)")
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[job {job_id}] FAILED: {tb}", flush=True)      # full detail to server logs (observability)
        job.update(status="error",                              # generic message to the client (no internals)
                   error=f"extraction failed (internal error; ref {job_id})",
                   traceback=tb[-1500:])                        # kept server-side only (not in _JOB_PUBLIC)
    finally:
        job["ended"] = time.time()
        job["started"] = job.get("started") or t0
        job["elapsed_s"] = round(job["ended"] - t0, 1)
        job["queue_wait_s"] = round(t0 - (job.get("created") or t0), 1)
        _save_meta(job_id)
        _log_experiment(job_id)


EXPERIMENT_LOG = REPO / "log" / "experiments.jsonl"
def _log_experiment(job_id: str):
    """Append ONE line per finished run to log/experiments.jsonl for easy cross-run stats (timing + tokens +
    cost + score). Never raises into the worker. The BYO api_key is never a JOBS field, so it cannot leak here."""
    try:
        job = JOBS.get(job_id) or {}
        rec = {k: job.get(k) for k in ("id", "filename", "mode", "status", "reason", "n_examples", "n_detections",
                                       "created", "started", "ended", "elapsed_s", "queue_wait_s",
                                       "tokens", "tokens_in", "tokens_out", "tokens_cached", "cost_usd",
                                       "iters", "provider", "model_name")}
        EXPERIMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(EXPERIMENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[job {job_id}] experiment-log write failed: {e}", flush=True)


# ─────────────────────────────── routes ───────────────────────────────
_JOB_PUBLIC = ("id", "status", "phase", "filename", "mode", "tokens", "tokens_in", "tokens_out", "tokens_cached",
               "cost_usd", "iters", "valid", "reason", "note", "n_detections", "n_examples", "reviewer_url",
               "elapsed_s", "error", "queue_position", "queue_workers", "provider", "model_name",
               "created", "started", "ended", "queue_wait_s")
# NOTE: the user's BYO LLM api_key is NEVER a JOBS field and NEVER in this projection (nor in _save_meta). It flows
# request -> _queue_job arg -> _pool.submit arg -> _worker local -> H.run local, living only on the worker stack.


def _queue_position(job_id: str):
    """How many jobs are ahead of this queued job (running + earlier-queued). 0 = next up. None = not queued.
    Lets the UI show 'queued, N ahead' instead of a silent multi-minute hang for the 3rd+ concurrent user."""
    job = JOBS.get(job_id)
    if not job or job.get("status") != "queued":
        return None
    created = job.get("created") or 0
    return sum(1 for j in list(JOBS.values())
               if j.get("status") in ("queued", "running") and (j.get("created") or 0) < created)


def _ui_bundle():
    """Return the product UI and whether it is the Document Mode bundle, not the legacy fallback.

    This is deliberately evaluated at request/health-check time: a process can outlive the temporary
    worktree it was launched from. In that state Python endpoints remain alive while the HTML file has
    disappeared, so a process-only health check would claim healthy while `/` silently degrades.
    """
    f = next((REPO / sub / "harness_index.html" for sub in ("product", "service")
              if (REPO / sub / "harness_index.html").exists()), None)
    if f is None:
        return _FALLBACK_HTML, False
    html = f.read_text()
    return html, ('id="docmode"' in html and "openDoc" in html)


@app.get("/", response_class=HTMLResponse)
def index():
    # release layout ships the UI at product/, the repo checkout at service/ — serve whichever exists
    html, _ = _ui_bundle()
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


def _queue_job(data: bytes, ext: str, filename: str, mode: str, owner: str,
               provider: str = "gemini-vertex", api_key: str = None, model_name: str = None,
               tag: str = "", conn: dict = None) -> str:
    """Persist one upload as an owned job and submit it to the worker pool. Shared by single + batch extract.
    provider/model_name are non-secret and stored on the job; api_key (BYO) is passed ONLY as a submit arg so it
    lives on the worker thread's stack -- it is never written into JOBS, meta.json, _JOB_PUBLIC, or any log.
    tag (optional, non-secret) = a free-form source label (e.g. GT1/GT2/pdf) shown in the history sidebar so a
    benchmark run's source mix is visible at a glance."""
    job_id = uuid.uuid4().hex[:12]
    jdir = JOBS_DIR / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    input_path = jdir / f"input{ext}"
    input_path.write_bytes(data)
    JOBS[job_id] = {"id": job_id, "status": "queued", "phase": "queued", "cancel": False,
                    "filename": filename, "mode": mode, "created": time.time(), "owner": owner,
                    "provider": provider, "model_name": model_name, "tag": (tag or "")[:24]}
    _save_meta(job_id)
    _pool.submit(_worker, job_id, input_path, filename or job_id, mode, provider, api_key, model_name, conn)
    return job_id


def _caller_owner(request: Request):
    """Identity of the caller; mint a guest session token if they have none. Returns (owner, new_cookie|None)."""
    owner = _identity(request)
    if owner:
        return owner, None
    tok = _new_session(None)
    return f"guest:{tok[:12]}", tok


def _runtime_caller_owner(request: Request):
    """Resolve the runtime tenant.

    A fixed owner is permitted only when the local launcher explicitly opts in.
    Hosted deployments leave CHEMRXN_LOCAL_RUNTIME_USER_ID unset and retain the
    signed cookie/account isolation used by the rest of the product.
    """
    if LOCAL_RUNTIME_USER_ID:
        return LOCAL_RUNTIME_USER_ID, None
    return _caller_owner(request)


def _valid_runtime_id(value: str) -> bool:
    return bool(value and len(value) <= 128
                and all(c.isalnum() or c in "_-" for c in value))


def _runtime_headers(owner: str, *, content_type: str = "application/json") -> dict:
    return {
        "Authorization": f"Bearer {PI_RUNTIME_INTERNAL_TOKEN}",
        "X-ChemRxn-User-Id": owner,
        "Accept": "application/json",
        "Content-Type": content_type,
    }


def _runtime_json(method: str, path: str, owner: str, body: dict | None = None,
                  timeout: int = 30) -> tuple[int, bytes, str]:
    """Call the loopback runtime without consulting proxy environment variables."""
    target = f"{PI_RUNTIME_BASE}{path}"
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    request = urllib.request.Request(
        target,
        method=method,
        data=payload,
        headers=_runtime_headers(owner),
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as upstream:
            return upstream.status, upstream.read(), upstream.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(), exc.headers.get_content_type()
        finally:
            exc.close()
    except Exception:
        raise HTTPException(503, "Interactive Pi backend not connected") from None


def _runtime_upload(upload: UploadFile, kind: str, owner: str) -> dict:
    """Stream one spooled multipart upload to the runtime; never buffer a PDF in RAM."""
    if kind not in ("figure", "paper", "si"):
        raise HTTPException(400, "Unknown runtime input kind")
    file_obj = upload.file
    file_obj.seek(0, os.SEEK_END)
    size = file_obj.tell()
    file_obj.seek(0)
    limit = MAX_RUNTIME_FIGURE_BYTES if kind == "figure" else MAX_RUNTIME_PDF_BYTES
    if size <= 0:
        raise HTTPException(400, f"{kind} input is empty")
    if size > limit:
        raise HTTPException(413, f"{kind} input exceeds the {limit // 1024 // 1024} MB limit")

    target = urllib.parse.urlsplit(f"{PI_RUNTIME_BASE}/v1/input-artifacts")
    if target.scheme not in ("http", "https") or not target.hostname:
        raise HTTPException(503, "Interactive Pi backend URL is invalid")
    connection_type = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(target.hostname, target.port, timeout=5 * 60)
    path = target.path + (f"?{target.query}" if target.query else "")
    media_type = (upload.content_type or "application/octet-stream").split(";", 1)[0]
    headers = _runtime_headers(owner, content_type=media_type)
    headers.update({
        "X-ChemRxn-Input-Kind": kind,
        "X-ChemRxn-File-Name": urllib.parse.quote(upload.filename or "upload", safe=""),
        "Content-Length": str(size),
    })
    try:
        connection.request("POST", path, body=file_obj, headers=headers)
        upstream = connection.getresponse()
        body = upstream.read()
        status = upstream.status
    except Exception:
        raise HTTPException(503, "Interactive Pi backend not connected") from None
    finally:
        connection.close()
        file_obj.seek(0)
    try:
        decoded = json.loads(body)
    except Exception:
        decoded = {}
    if status >= 400:
        message = decoded.get("error", {}).get("message") if isinstance(decoded, dict) else None
        raise HTTPException(status if status in (400, 403, 413) else 502,
                            message or "Runtime input upload was rejected")
    artifact = decoded.get("artifact") if isinstance(decoded, dict) else None
    if not isinstance(artifact, dict) or not artifact.get("id"):
        raise HTTPException(502, "Runtime returned an invalid input artifact")
    return artifact


def _runtime_result_and_artifact(
    session_id: str,
    owner: str,
):
    status, result_bytes, _ = _runtime_json(
        "GET", f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/result", owner,
    )
    try:
        result = json.loads(result_bytes)
    except Exception:
        result = {}
    if status >= 400 or not isinstance(result, dict):
        raise HTTPException(status if status in (400, 403, 404, 409) else 502,
                            "Runtime result is not available")
    if result.get("gtIncluded") is not False or b"GT_SENTINEL" in result_bytes:
        raise HTTPException(409, "Runtime result failed GT isolation")
    if result.get("schemaVersion") != "chemrxn.runtime-result.v1":
        raise HTTPException(409, "Unsupported runtime result contract")

    manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
    artifact_by_id = {
        item.get("id"): item
        for item in (manifest.get("artifacts") or [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }

    def fetch_artifact(artifact_id: str) -> tuple[bytes, dict]:
        meta = artifact_by_id.get(artifact_id)
        if not meta:
            raise HTTPException(409, f"Runtime artifact metadata is missing: {artifact_id}")
        path = (
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/artifacts/"
            f"{urllib.parse.quote(artifact_id, safe='')}"
        )
        artifact_status, artifact_bytes, _ = _runtime_json("GET", path, owner, timeout=60)
        if artifact_status >= 400:
            raise HTTPException(artifact_status if artifact_status in (403, 404) else 502,
                                f"Runtime artifact is unavailable: {artifact_id}")
        expected = meta.get("sha256")
        actual = hashlib.sha256(artifact_bytes).hexdigest()
        if not isinstance(expected, str) or not secrets.compare_digest(expected, actual):
            raise HTTPException(409, f"Runtime artifact hash mismatch: {artifact_id}")
        if b"GT_SENTINEL" in artifact_bytes:
            raise HTTPException(409, "Runtime artifact failed GT isolation")
        return artifact_bytes, meta

    return result, result_bytes, fetch_artifact


def _pi_provider_for_web(registry_id: str, resolved_provider: str, conn: dict) -> str:
    """Limit Web launch to official Pi providers until custom base URLs are part of its contract."""
    if conn:
        raise HTTPException(409, "Custom model gateways are not connected to the Pi runtime yet")
    mapping = {
        "byo-openai": "openai",
        "openai": "openai",
        "byo-anthropic": "anthropic",
        "anthropic": "anthropic",
        "byo-gemini": "google",
        "gemini-api": "google",
    }
    pi_provider = mapping.get(registry_id) or mapping.get(resolved_provider)
    access, _ = _model_access(registry_id)
    if access != "byo" or not pi_provider:
        raise HTTPException(
            409,
            "Live Pi Web extraction currently supports official BYO OpenAI, Anthropic, or Gemini keys",
        )
    return pi_provider


@app.get("/api/runtime/capabilities")
def api_runtime_capabilities():
    """Expose only non-secret local runtime readiness to the product upload card."""
    disconnected = {
        "connected": False,
        "extractionBackend": "not_connected",
        "interactivePiBackend": "not_connected",
        "inputs": {"figure": False, "paper": False, "paper-si": False},
        "message": "Extraction backend not connected. Preview with demo paper.",
    }
    if not PI_RUNTIME_INTERNAL_TOKEN:
        return disconnected
    request = urllib.request.Request(
        f"{PI_RUNTIME_BASE}/healthz",
        headers={"Accept": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=2) as upstream:
            health = json.loads(upstream.read())
    except Exception:
        return disconnected
    if not isinstance(health, dict):
        return disconnected
    extraction_backend = health.get("extractionBackend")
    interactive_backend = health.get("interactivePiBackend")
    connected = (
        extraction_backend == "figure_live_ready"
        and interactive_backend == "local_api_key_or_pi_auth_ready"
    )
    return {
        "connected": connected,
        "extractionBackend": extraction_backend or "not_connected",
        "interactivePiBackend": interactive_backend or "not_connected",
        "inputs": {
            "figure": connected,
            "paper": False,
            "paper-si": False,
        },
        "message": (
            "Reaction image extraction is connected."
            if connected else disconnected["message"]
        ),
    }


@app.get("/api/runtime/auth/status")
def api_runtime_auth_status(request: Request):
    """Return only whether the isolated ChemRxn Pi profile can authenticate."""
    if not PI_RUNTIME_INTERNAL_TOKEN:
        return {
            "configured": False,
            "provider": "openai-codex",
            "profile": "chemrxn_isolated",
            "loginCommand": "./chemrxn login",
        }
    owner, new_cookie = _runtime_caller_owner(request)
    status, body, _ = _runtime_json("GET", "/v1/auth/status", owner, timeout=10)
    try:
        decoded = json.loads(body)
    except Exception:
        decoded = {}
    if status >= 400 or not isinstance(decoded, dict):
        raise HTTPException(503, "Pi auth status is unavailable")
    response = JSONResponse({
        "configured": decoded.get("configured") is True,
        "provider": "openai-codex",
        "profile": "chemrxn_isolated",
        "loginCommand": "./chemrxn login",
    })
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
    return response


@app.post("/api/runtime/models/resolve")
async def api_runtime_model_resolution(request: Request):
    """Validate a browser-selected official Pi model before transmitting source bytes."""
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Model preflight requires a JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Model preflight requires a JSON object")
    provider_id = payload.get("provider")
    model_name = _clean_model(payload.get("modelId") if isinstance(payload.get("modelId"), str) else "")
    require_image = payload.get("requireImage") is True
    if not isinstance(provider_id, str) or not model_name:
        raise HTTPException(400, "Provider and model are required")
    pi_provider = _pi_provider_for_web(provider_id, provider_id, {})
    owner, new_cookie = _runtime_caller_owner(request)
    status, body, media_type = await run_in_threadpool(
        _runtime_json,
        "POST",
        "/v1/models/resolve",
        owner,
        {
            "provider": pi_provider,
            "modelId": model_name,
            "requireImage": require_image,
        },
        10,
    )
    response = Response(content=body, status_code=status, media_type=media_type, headers={
        "Cache-Control": "no-cache, no-store",
        "X-Content-Type-Options": "nosniff",
    })
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
    return response


@app.post("/api/runtime/extractions")
async def api_runtime_extraction(
    request: Request,
    source_type: str = Form(...),
    primary: UploadFile = File(...),
    si: Optional[UploadFile] = File(None),
    auth_mode: str = Form("pi_auth"),
    llm_provider: str = Form(""),
    llm_key: str = Form(""),
    llm_model: str = Form(""),
    llm_base_url: str = Form(""),
    llm_endpoint: str = Form(""),
    llm_api_version: str = Form(""),
    thinking_level: str = Form("medium"),
    initial_message: str = Form(""),
):
    """Create the real local Pi lifecycle from the shared product upload card."""
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    if source_type != "figure" or si is not None:
        raise HTTPException(400, "Only a single reaction image is supported")
    context_mode, primary_kind = "figure", "figure"
    if auth_mode not in ("pi_auth", "api_key"):
        raise HTTPException(400, "auth_mode must be pi_auth or api_key")
    if thinking_level not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise HTTPException(
            400,
            "thinking_level must be off, minimal, low, medium, high, xhigh, or max",
        )

    owner, new_cookie = _runtime_caller_owner(request)
    if auth_mode == "pi_auth":
        auth_status, auth_body, _ = await run_in_threadpool(
            _runtime_json, "GET", "/v1/auth/status", owner, None, 10,
        )
        try:
            auth_decoded = json.loads(auth_body)
        except Exception:
            auth_decoded = {}
        if auth_status >= 400 or auth_decoded.get("configured") is not True:
            raise HTTPException(
                409,
                "Pi auth is not configured. Run `./chemrxn login` in this repository first.",
            )
        pi_provider, model_name, api_key = "openai-codex", llm_model.strip() or "gpt-5.6-sol", ""
    else:
        if not llm_key.strip():
            raise HTTPException(
                400,
                "Enter an API key for this extraction. Product API keys are never loaded from the legacy session vault.",
            )
        requested_model = llm_model.strip()
        if requested_model and not _clean_model(llm_model):
            raise HTTPException(
                400,
                "Model identifiers must be 1–80 characters using only letters, numbers, . - _ : or /",
            )
        resolved_provider, api_key, model_name, conn = _resolve_llm(
            request, llm_provider, llm_key, llm_model,
            llm_base_url, llm_endpoint, llm_api_version,
        )
        if not api_key:
            raise HTTPException(409, "This model does not expose a per-user key to the local Pi runtime")
        pi_provider = _pi_provider_for_web(llm_provider, resolved_provider, conn)

    model_status, model_body, _ = await run_in_threadpool(
        _runtime_json,
        "POST",
        "/v1/models/resolve",
        owner,
        {
            "provider": pi_provider,
            "modelId": model_name,
            "requireImage": source_type == "figure",
        },
        10,
    )
    if model_status >= 400:
        try:
            model_decoded = json.loads(model_body)
        except Exception:
            model_decoded = {}
        model_error = model_decoded.get("error") if isinstance(model_decoded, dict) else {}
        message = model_error.get("message") if isinstance(model_error, dict) else None
        raise HTTPException(
            model_status if model_status in (400, 409, 503) else 502,
            message or "The selected model is unavailable in the local Pi runtime",
        )

    primary_artifact = await run_in_threadpool(_runtime_upload, primary, primary_kind, owner)
    artifacts = [{"artifactId": primary_artifact["id"], "kind": primary_kind}]

    extraction_id = uuid.uuid4().hex
    body = {
        "userId": owner,
        "jobId": f"web-extract:{extraction_id}",
        "contextMode": context_mode,
        "workspaceId": f"web-{extraction_id}",
        "provider": pi_provider,
        "modelId": model_name,
        "thinkingLevel": thinking_level,
        "authMode": auth_mode,
        "inputs": artifacts,
        "startExtraction": True,
    }
    if auth_mode == "api_key":
        body["apiKey"] = api_key
    if initial_message.strip():
        body["initialMessage"] = initial_message.strip()[:20_000]
    status, response_body, _ = await run_in_threadpool(
        _runtime_json, "POST", "/v1/local-agent-sessions", owner, body, 60,
    )
    try:
        decoded = json.loads(response_body)
    except Exception:
        decoded = {}
    if status >= 400:
        message = decoded.get("error", {}).get("message") if isinstance(decoded, dict) else None
        raise HTTPException(status if status in (400, 403, 409, 503) else 502,
                            message or "Runtime session creation was rejected")
    session = decoded.get("session") if isinstance(decoded, dict) else None
    if not isinstance(session, dict) or not _valid_runtime_id(session.get("id", "")):
        raise HTTPException(502, "Runtime returned an invalid session")
    response = JSONResponse({
        "jobId": session["id"],
        "sessionId": session["id"],
        "session": session,
        "contextMode": context_mode,
        "inputs": [primary_artifact] + ([si_artifact] if si is not None else []),
    }, status_code=201)
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
    return response


@app.post("/api/runtime/sessions/{session_id}/reviewer")
def api_runtime_register_reviewer(session_id: str, request: Request):
    """Register immutable live or explicitly packaged replay artifacts into shared renderers."""
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    if not _valid_runtime_id(session_id):
        raise HTTPException(404, "runtime session not found")
    owner, new_cookie = _runtime_caller_owner(request)
    result, result_bytes, fetch_artifact = _runtime_result_and_artifact(session_id, owner)
    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    source_mode = source.get("mode")
    packaged_replay = (
        source_mode == "sealed_replay"
        and source.get("reconstructed") is True
        and isinstance(result.get("viewer"), dict)
    )
    if source_mode != "live_pi_sdk" and not packaged_replay:
        raise HTTPException(409, "Result has no packaged visual workspace artifacts")
    if result.get("domainContract") != "chemrxn.figure-job.v1":
        raise HTTPException(409, "Unsupported reviewer contract")
    viewer = result.get("viewer") if isinstance(result.get("viewer"), dict) else {}
    input_id = viewer.get("inputArtifactId")
    prediction_id = viewer.get("predictionArtifactId")
    if not isinstance(input_id, str) or not isinstance(prediction_id, str):
        raise HTTPException(409, "Live Figure result is missing reviewer artifacts")
    input_bytes, input_meta = fetch_artifact(input_id)
    prediction_bytes, _ = fetch_artifact(prediction_id)
    try:
        prediction = json.loads(prediction_bytes)
    except Exception:
        raise HTTPException(409, "Prediction artifact is not valid JSON") from None
    if not isinstance(prediction, dict):
        raise HTTPException(409, "Prediction artifact must be a V2 object")

    job_id = f"runtime_{hashlib.sha256(session_id.encode()).hexdigest()[:12]}"
    slug, tid = f"upload_{job_id}", "0"
    jdir = JOBS_DIR / job_id
    work = jdir / "work"
    out_dir = PDF_ROOT / slug
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    media_type = input_meta.get("mediaType")
    extension = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/tiff": ".tiff",
    }.get(media_type, ".img")
    source_path = jdir / f"input{extension}"
    source_path.write_bytes(input_bytes)
    _to_png(source_path, work / "0.png")
    shutil.copyfile(work / "0.png", out_dir / f"{tid}.png")

    # Preserve the runtime prediction byte-for-byte. Normalization is a derived
    # reviewer copy only; the immutable source artifact and result.json never change.
    (jdir / "result.json").write_bytes(prediction_bytes)
    (jdir / "runtime_result.json").write_bytes(result_bytes)
    normalized = _normalize_v2(copy.deepcopy(prediction), slug, tid)
    manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
    model = manifest.get("model") if isinstance(manifest.get("model"), dict) else {}
    normalized["extractor"] = (
        f"Pi SDK reaction-extract ({model.get('provider', 'unknown')}/"
        f"{model.get('id', 'unknown')})"
    )
    (out_dir / f"{tid}_predicted_output.json").write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    out_dir.joinpath("reactions.json").write_text(
        json.dumps({
            "paper_id": slug,
            "title": "Live Pi Figure extraction",
            "page_count": 1,
            "extractor": normalized["extractor"],
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), list) else []
    by_kind = {
        item.get("kind"): item.get("id")
        for item in artifacts
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if isinstance(by_kind.get("molparser_raw"), str):
        raw_bytes, _ = fetch_artifact(by_kind["molparser_raw"])
        (work / "0_molparser.json").write_bytes(raw_bytes)
        # FigureReviewIsland reads evidence through the shared reviewer server,
        # whose authoritative raw-evidence convention is
        # `<task-id>_molparser.json` beside the normalized prediction. Keep the
        # job work copy for legacy diagnostics, and mirror the exact same bytes
        # into the one renderer's data root so bbox overlays never fall back to
        # final-V2-derived pseudo provenance.
        (out_dir / f"{tid}_molparser.json").write_bytes(raw_bytes)
    overview_id = viewer.get("overviewArtifactId")
    if isinstance(overview_id, str):
        overview_bytes, _ = fetch_artifact(overview_id)
        (work / "0_annotated.png").write_bytes(overview_bytes)

    now = time.time()
    JOBS[job_id] = {
        "id": job_id,
        "status": "done" if result.get("terminalStatus") == "review_ready" else "incomplete",
        "phase": "done",
        "filename": input_meta.get("locator") or "live-figure",
        "mode": "fast",
        "created": now,
        "started": now,
        "ended": now,
        "owner": owner,
        "provider": model.get("provider"),
        "model_name": model.get("id"),
        "valid": result.get("terminalStatus") == "review_ready",
        "reason": None if result.get("terminalStatus") == "review_ready" else "runtime-partial",
        "n_examples": len(prediction.get("selected_examples") or []),
        "n_detections": manifest.get("cache", {}).get("molparser", {}).get("detectionCount"),
        "reviewer_url": f"{REVIEWER_BASE}/#/{slug}/{tid}",
    }
    _save_meta(job_id)
    response = JSONResponse({
        "jobId": job_id,
        "runtimeSessionId": session_id,
        "workspaceUrl": f"/?job={urllib.parse.quote(job_id)}&runtime_session={urllib.parse.quote(session_id)}",
        "reviewerUrl": JOBS[job_id]["reviewer_url"],
        "sourcePredictionSha256": hashlib.sha256(prediction_bytes).hexdigest(),
    })
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
    return response


def _runtime_pdf_anchor_source(anchor):
    if not isinstance(anchor, dict):
        return "main"
    source = anchor.get("_source") if isinstance(anchor.get("_source"), dict) else {}
    value = source.get("source_document") or source.get("document") or anchor.get("source_document")
    return "si" if str(value or "main").lower() == "si" else "main"


def _reconcile_pdf_view_anchor_index(view, prediction):
    """Return `view` with anchors[].index in the GLOBAL, main-first namespace.

    Some derived pdf_view.json emit a per-document LOCAL anchor index (main 0..m-1, si 0..s-1)
    while every OTHER anchor reference in the same file is already GLOBAL (molecules[].anchor,
    text_reactions[].anchor, text_claims[].anchor -> main 0..m-1, si m..m+s-1), which is also
    the space the reviewer_v2 rows use. That split makes an SI reaction (global anchor >= m)
    resolve to a main anchor (local index < m) -> clicking an SI reaction jumps back to the main
    text. We re-derive each anchor's index from the prediction's anchor order (the canonical
    global order) so the whole reviewer speaks one index space.

    Order-independent and idempotent. Each source is classified once: if every one of that
    source's view anchors already carries a valid GLOBAL index for that source, the view is left
    untouched (so an already-global -- possibly reordered -- view is a no-op). Otherwise the view is
    LOCAL and each anchor is mapped by its OWN local index (`positions[source][local]`), the exact
    rule reviewer_v2 uses, so gaps or reordering can't relabel a neighbour. A no-op when the
    prediction or view anchors are missing/malformed, or a source's index is out of range.
    """
    if not isinstance(view, dict):
        return view
    view_anchors = view.get("anchors")
    pred_anchors = (prediction or {}).get("anchors")
    if not isinstance(view_anchors, list) or not isinstance(pred_anchors, list):
        return view
    positions = {"main": [], "si": []}
    for global_pos, anchor in enumerate(pred_anchors):
        positions[_runtime_pdf_anchor_source(anchor)].append(global_pos)
    global_sets = {source: set(sequence) for source, sequence in positions.items()}

    def _source_of(anchor):
        return "si" if str(anchor.get("source_document") or "main").lower() == "si" else "main"

    # A source is already global when EVERY one of its view anchors sits in that source's global
    # position set. In a local view the SI anchors restart at 0, which collides with the main range
    # and so can never all land in the SI global set -> it is (correctly) classified local.
    already_global = {}
    for source in ("main", "si"):
        source_anchors = [a for a in view_anchors if isinstance(a, dict) and _source_of(a) == source]
        already_global[source] = bool(source_anchors) and all(
            a.get("index") in global_sets[source] for a in source_anchors)

    reconciled = []
    changed = False
    for anchor in view_anchors:
        if not isinstance(anchor, dict):
            reconciled.append(anchor)
            continue
        source = _source_of(anchor)
        sequence = positions.get(source, [])
        local = anchor.get("index")
        if (already_global.get(source) or not isinstance(local, int)
                or not (0 <= local < len(sequence))):
            reconciled.append(anchor)
            continue
        global_index = sequence[local]
        if global_index != local:
            anchor = {**anchor, "index": global_index, "index_local": local}
            changed = True
        reconciled.append(anchor)
    if not changed:
        return view
    out = dict(view)
    out["anchors"] = reconciled
    return out


def _load_reconciled_pdf_view(jdir):
    """Read work/pdf_view.json and normalise its anchor index to the global namespace so the
    reviewer, the /pdf/anchor endpoints and the island all resolve anchors in one space."""
    view = _read_json(jdir / "work" / "pdf_view.json")
    if view is None:
        return None
    return _reconcile_pdf_view_anchor_index(view, _read_json(jdir / "result.json"))


def _runtime_pdf_reviewer_data(pdf_view_adapter, prediction, pdf_view, perception_roots):
    """Build one evidence index while keeping main/SI raw joins in their own namespaces."""
    anchors = prediction.get("anchors") if isinstance(prediction.get("anchors"), list) else []
    view_anchors = pdf_view.get("anchors") if isinstance(pdf_view.get("anchors"), list) else []
    rows = []
    for source_document in ("main", "si"):
        indexed = [
            (index, anchor)
            for index, anchor in enumerate(anchors)
            if _runtime_pdf_anchor_source(anchor) == source_document
        ]
        if not indexed:
            continue
        source_prediction = dict(prediction)
        source_prediction["anchors"] = [anchor for _, anchor in indexed]
        by_global = {
            item.get("index"): item
            for item in view_anchors
            if isinstance(item, dict) and item.get("source_document", "main") == source_document
        }
        source_view_anchors = [by_global.get(index, {}) for index, _ in indexed]
        source_data = pdf_view_adapter._reviewer_v2_data(  # noqa: SLF001
            source_prediction,
            perception_roots,
            source_view_anchors,
        )
        for row in source_data.get("rows") or []:
            if not isinstance(row, dict) or not isinstance(row.get("anchor"), int):
                continue
            local_anchor = row["anchor"]
            if local_anchor < 0 or local_anchor >= len(indexed):
                continue
            global_anchor = indexed[local_anchor][0]
            reaction_index = row.get("reaction")
            row["anchor"] = global_anchor
            row["id"] = f"pred:{global_anchor}:{reaction_index}"
            row["source_document"] = source_document
            for side in ("reactants", "products"):
                role = "reactant" if side == "reactants" else "product"
                for molecule_index, molecule in enumerate(row.get(side) or []):
                    if not isinstance(molecule, dict):
                        continue
                    occurrence = molecule.get("occurrence")
                    if not isinstance(occurrence, dict):
                        continue
                    occurrence.update({
                        "anchor_id": f"{source_document}:a{global_anchor}",
                        "anchor": global_anchor,
                        "reaction_id": f"pred:{global_anchor}:{reaction_index}",
                        "occurrence_id": (
                            f"{source_document}:a{global_anchor}:r{reaction_index}:"
                            f"{role}:{molecule_index}"
                        ),
                        "molecule_path": [
                            "anchors", global_anchor, "selected_examples",
                            reaction_index, side, molecule_index, "E-SMILES",
                        ],
                    })
            rows.append(row)
    return {
        "schema": "pdf_reviewer_v2_evidence_v1",
        "paper_id": str(prediction.get("paper_id") or ""),
        "rows": rows,
    }


@app.get("/api/runtime/sessions")
def api_runtime_sessions(request: Request):
    """Tenant-scoped product history; reviewer artifacts remain children of a session."""
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    owner, new_cookie = _runtime_caller_owner(request)
    status, body, media_type = _runtime_json("GET", "/v1/sessions", owner)
    response = Response(content=body, status_code=status, media_type=media_type, headers={
        "Cache-Control": "no-cache, no-store",
        "X-Content-Type-Options": "nosniff",
    })
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
    return response


@app.get("/api/runtime/sessions/{session_id}")
def api_runtime_session(session_id: str, request: Request):
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    if not _valid_runtime_id(session_id):
        raise HTTPException(404, "runtime session not found")
    owner, new_cookie = _runtime_caller_owner(request)
    status, body, media_type = _runtime_json(
        "GET", f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}", owner,
    )
    response = Response(content=body, status_code=status, media_type=media_type, headers={
        "Cache-Control": "no-cache, no-store",
        "X-Content-Type-Options": "nosniff",
    })
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
    return response


@app.get("/api/runtime/sessions/{session_id}/dispatch")
def api_runtime_dispatch(session_id: str, request: Request):
    """Return tenant-scoped durable queue truth without exposing worker leases."""
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    if not _valid_runtime_id(session_id):
        raise HTTPException(404, "runtime session not found")
    owner, new_cookie = _runtime_caller_owner(request)
    path = f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/dispatch"
    status, body, media_type = _runtime_json("GET", path, owner)
    response = Response(content=body, status_code=status, media_type=media_type, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
    return response


@app.get("/api/runtime/input-artifacts/{artifact_id}")
def api_runtime_input_artifact(artifact_id: str, request: Request):
    """Tenant-authorized immutable input preview available before sealing."""
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    if not _valid_runtime_id(artifact_id):
        raise HTTPException(404, "runtime input artifact not found")
    owner, new_cookie = _runtime_caller_owner(request)
    path = f"/v1/input-artifacts/{urllib.parse.quote(artifact_id, safe='')}"
    status, body, media_type = _runtime_json("GET", path, owner, timeout=60)
    response = Response(content=body, status_code=status, media_type=media_type, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
    return response


@app.get("/api/runtime/sessions/{session_id}/live-artifacts/{kind}")
def api_runtime_live_artifact(session_id: str, kind: str, request: Request):
    """Serve allowlisted, read-only Figure or PDF evidence while Pi is running."""
    allowed = {"overview", "ocsr-grid", "molparser", "candidate"}
    pdf_kind = re.fullmatch(
        r"pdf-(main|si)-(layout|uniparser|candidate|page-\d{1,3}|molparser-\d{1,3})",
        kind,
    )
    pdf_checkpoint = re.fullmatch(r"pdf-checkpoint-[a-f0-9]{64}", kind)
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    if not _valid_runtime_id(session_id) or (
        kind not in allowed and not pdf_kind and not pdf_checkpoint
    ):
        raise HTTPException(404, "runtime live artifact not found")
    owner, new_cookie = _runtime_caller_owner(request)
    path = (
        f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/live-artifacts/"
        f"{urllib.parse.quote(kind, safe='')}"
    )
    status, body, media_type = _runtime_json("GET", path, owner, timeout=60)
    response = Response(content=body, status_code=status, media_type=media_type, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'none'",
    })
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
    return response


@app.get("/api/runtime/sessions/{session_id}/artifacts/{artifact_id}")
def api_runtime_artifact(session_id: str, artifact_id: str, request: Request):
    """Tenant-authorized binary artifact bridge for the existing visual workspace."""
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    if not _valid_runtime_id(session_id) or not artifact_id or len(artifact_id) > 512:
        raise HTTPException(404, "runtime artifact not found")
    owner, new_cookie = _runtime_caller_owner(request)
    path = (
        f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/artifacts/"
        f"{urllib.parse.quote(artifact_id, safe='')}"
    )
    status, body, media_type = _runtime_json("GET", path, owner, timeout=60)
    response = Response(content=body, status_code=status, media_type=media_type, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
    return response


@app.get("/api/runtime/sessions/{session_id}/event-log")
def api_runtime_event_log(session_id: str, request: Request, after: int = 0):
    """Return one bounded persisted-event snapshot before live SSE begins.

    Historical workspace commands are reconstructed as clickable citations but
    are not replayed as live navigation. The subsequent SSE starts at the
    returned durable cursor, so events created during hydration are not lost.
    """
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    if not _valid_runtime_id(session_id):
        raise HTTPException(404, "runtime session not found")
    if after < 0:
        raise HTTPException(400, "after must be a non-negative integer")
    owner, new_cookie = _runtime_caller_owner(request)
    path = (
        f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/events"
        f"?after={after}"
    )
    status, body, media_type = _runtime_json("GET", path, owner)
    response = Response(content=body, status_code=status, media_type=media_type, headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
    return response


@app.get("/api/runtime/sessions/{session_id}/events")
def api_runtime_event_stream(session_id: str, request: Request, after: int = 0):
    """Same-origin SSE bridge to the internal Pi Runtime.

    The browser authenticates with the product's HttpOnly session cookie. The
    internal bearer token never enters HTML, JavaScript, URL parameters, logs,
    or browser storage. Last-Event-ID is forwarded so native EventSource
    reconnect resumes from the durable runtime cursor.
    """
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    if not session_id or len(session_id) > 128 or not all(c.isalnum() or c in "_-" for c in session_id):
        raise HTTPException(404, "runtime session not found")
    if after < 0:
        raise HTTPException(400, "after must be a non-negative integer")
    last_event_id = request.headers.get("last-event-id", "")
    if last_event_id and not last_event_id.isdigit():
        raise HTTPException(400, "Last-Event-ID must be a non-negative integer")
    owner, new_cookie = _runtime_caller_owner(request)
    target = (
        f"{PI_RUNTIME_BASE}/v1/sessions/{urllib.parse.quote(session_id, safe='')}/stream"
        f"?after={after}"
    )

    headers = {
        "Authorization": f"Bearer {PI_RUNTIME_INTERNAL_TOKEN}",
        "X-ChemRxn-User-Id": owner,
        "Accept": "text/event-stream",
    }
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    upstream_request = urllib.request.Request(target, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        upstream_response = opener.open(upstream_request, timeout=35)
    except urllib.error.HTTPError as exc:
        status = exc.code if exc.code in (400, 401, 403, 404) else 502
        exc.close()
        raise HTTPException(status, "Runtime event stream was rejected.") from None
    except Exception:
        raise HTTPException(503, "Interactive Pi backend not connected") from None
    if upstream_response.status == 204:
        upstream_response.close()
        return Response(status_code=204)

    def event_bytes():
        with upstream_response:
            while True:
                line = upstream_response.readline()
                if not line:
                    break
                yield line

    response = StreamingResponse(event_bytes(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-store",
        "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
    })
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


@app.get("/api/runtime/sessions/{session_id}/result")
def api_runtime_result(session_id: str, request: Request):
    """Return one tenant-authorized runtime result without exposing service auth."""
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    if not session_id or len(session_id) > 128 or not all(c.isalnum() or c in "_-" for c in session_id):
        raise HTTPException(404, "runtime session not found")
    owner, new_cookie = _runtime_caller_owner(request)
    target = (
        f"{PI_RUNTIME_BASE}/v1/sessions/{urllib.parse.quote(session_id, safe='')}/result"
    )
    upstream_request = urllib.request.Request(target, headers={
        "Authorization": f"Bearer {PI_RUNTIME_INTERNAL_TOKEN}",
        "X-ChemRxn-User-Id": owner,
        "Accept": "application/json",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(upstream_request, timeout=10) as upstream:
            body = upstream.read()
    except urllib.error.HTTPError as exc:
        status = exc.code if exc.code in (400, 401, 403, 404, 409) else 502
        exc.close()
        raise HTTPException(status, "Runtime result request was rejected.") from None
    except Exception:
        raise HTTPException(503, "Interactive Pi backend not connected") from None
    response = Response(content=body, media_type="application/json", headers={
        "Cache-Control": "no-cache, no-store",
        "X-Content-Type-Options": "nosniff",
    })
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


@app.post("/api/runtime/sessions/{session_id}/messages")
async def api_runtime_message(session_id: str, request: Request):
    """Send one prompt/steer/follow-up through the tenant-authorized local runtime."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON message body") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "Message body must be an object")
    text = body.get("text")
    behavior = body.get("behavior", "prompt")
    api_key = body.get("apiKey")
    if not isinstance(text, str) or not text.strip() or len(text) > 20_000:
        raise HTTPException(400, "Message text must contain 1–20000 characters")
    if behavior not in ("prompt", "steer", "follow_up"):
        raise HTTPException(400, "Unknown message behavior")
    if api_key is not None and (
        not isinstance(api_key, str) or not 8 <= len(api_key) <= 16_384
    ):
        raise HTTPException(400, "API key must contain 8–16384 characters")
    payload = {"text": text, "behavior": behavior}
    if api_key:
        payload["apiKey"] = api_key
    return _runtime_post(
        session_id,
        request,
        "messages",
        payload,
    )


@app.post("/api/runtime/sessions/{session_id}/credentials")
async def api_runtime_queue_credentials(session_id: str, request: Request):
    """Forward one BYO key into memory for a restart-blocked queued run.

    The key is deliberately accepted only on this narrow endpoint and is never
    copied into a cookie, URL, runtime event, response, or product-side store.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON credential body") from None
    if not isinstance(body, dict) or set(body) != {"apiKey"}:
        raise HTTPException(400, "Credential body must contain only apiKey")
    api_key = body.get("apiKey")
    if not isinstance(api_key, str) or not 8 <= len(api_key) <= 16_384:
        raise HTTPException(400, "API key must contain 8–16384 characters")
    return _runtime_post(
        session_id,
        request,
        "credentials",
        {"apiKey": api_key},
    )


@app.post("/api/runtime/sessions/{session_id}/review-outcomes")
async def api_runtime_review_outcome(session_id: str, request: Request):
    """Persist one bounded human disposition for a prior Pi workspace proposal."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON review outcome") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "Review outcome must be an object")
    allowed = {
        "proposalId", "jobId", "target", "status",
        "reviewedVersion", "patchId", "reason",
    }
    if set(body) - allowed:
        raise HTTPException(400, "Review outcome contains unsupported fields")
    body = {
        key: value for key, value in body.items()
        if value is not None
    }
    allowed_reasons = {
        "human_edits_present",
        "superseded",
        "matches_review_head",
        "preview_failed",
        "human_edit_replaced",
        "review_head_changed",
        "human_closed",
        "workspace_target_mismatch",
        "reaction_not_found",
        "benchmark_read_only",
        "proposal_not_stageable",
        "human_navigation",
        "human_review_replaced",
        "human_discarded",
    }
    proposal_id = body.get("proposalId")
    job_id = body.get("jobId")
    if (
        not isinstance(proposal_id, str)
        or not 1 <= len(proposal_id) <= 200
        or not all(c.isalnum() or c in "._:-" for c in proposal_id)
    ):
        raise HTTPException(400, "Invalid proposal ID")
    if (
        not isinstance(job_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", job_id)
    ):
        raise HTTPException(400, "Invalid review outcome job")
    if body.get("target") not in ("figure", "pdf"):
        raise HTTPException(400, "Invalid review target")
    if body.get("status") not in ("staged", "applied", "discarded", "expired", "rejected"):
        raise HTTPException(400, "Invalid review outcome status")
    version = body.get("reviewedVersion")
    if body["status"] == "applied":
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise HTTPException(400, "Applied review outcome requires reviewedVersion")
        if not isinstance(body.get("patchId"), str) or not body["patchId"].strip():
            raise HTTPException(400, "Applied review outcome requires patchId")
    elif version is not None or body.get("patchId") is not None:
        raise HTTPException(400, "Only applied outcomes may carry version metadata")
    for field, maximum in (("patchId", 256),):
        value = body.get(field)
        if value is not None and (
            not isinstance(value, str) or not value.strip() or len(value) > maximum
        ):
            raise HTTPException(400, f"Invalid {field}")
    if body.get("reason") is not None and body["reason"] not in allowed_reasons:
        raise HTTPException(400, "Invalid review outcome reason")
    caller = _runtime_caller_owner(request)
    if body["status"] == "applied":
        _verify_applied_review_outcome(body, caller[0])
    return _runtime_post(
        session_id, request, "review-outcomes", body, caller=caller,
    )


def _verify_applied_review_outcome(body: dict, owner: str = None) -> None:
    """Bind an `applied` chat claim to an immutable reviewer snapshot.

    Browser events may request lifecycle recording, but they are not proof that
    a write happened.  Only a snapshot already committed by the Figure/PDF
    reviewer can authorize an applied runtime outcome.
    """
    job_id = body["jobId"]
    version = body["reviewedVersion"]
    patch_id = body["patchId"]
    agent_proposal_id = body["proposalId"]
    if body["target"] == "pdf":
        jdir, _ = _pdf_job(job_id, owner=owner)
        snapshot = _pdf_review_snapshot(jdir, version)
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("patch_id") != patch_id
            or snapshot.get("agent_proposal_id") != agent_proposal_id
        ):
            raise HTTPException(409, "Applied PDF review snapshot does not match this outcome")
        return

    meta = _read_json(JOBS_DIR / job_id / "meta.json") or JOBS.get(job_id) or {}
    _require_job_owner(meta, owner=owner)
    reviewer_url = meta.get("reviewer_url")
    match = re.search(r"/#/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)(?:[/?#]|$)",
                      reviewer_url if isinstance(reviewer_url, str) else "")
    if not match:
        raise HTTPException(409, "Figure reviewer identity is unavailable")
    slug, task_id = match.groups()
    if slug in (".", "..") or task_id in (".", ".."):
        raise HTTPException(409, "Figure reviewer identity is invalid")
    snapshot_path = (
        PDF_ROOT / slug
        / f"{task_id}_predicted_output.reviewed-v{version}.json"
    )
    snapshot = _read_json(snapshot_path)
    reviewed_meta = (
        snapshot.get("_reviewed_meta")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("_reviewed_meta"), dict)
        else {}
    )
    if (
        reviewed_meta.get("version") != version
        or reviewed_meta.get("proposal_id") != patch_id
        or reviewed_meta.get("agent_proposal_id") != agent_proposal_id
        or reviewed_meta.get("slug") != slug
        or str(reviewed_meta.get("task_id")) != task_id
    ):
        raise HTTPException(409, "Applied Figure review snapshot does not match this outcome")


@app.post("/api/runtime/sessions/{session_id}/abort")
def api_runtime_abort(session_id: str, request: Request):
    """Abort the active Pi turn without exposing the internal runtime credential."""
    return _runtime_post(session_id, request, "abort", {})


@app.post("/api/runtime/sessions/{session_id}/retry")
def api_runtime_retry(session_id: str, request: Request):
    """Create a new isolated attempt without exposing or persisting Pi credentials.

    Pi-auth sessions can retry directly. A BYO session whose short-lived
    in-memory lease has expired returns the runtime's honest credential error;
    the browser never receives or forwards an API key.
    """
    return _runtime_post(session_id, request, "retry", {}, timeout=35)


def _runtime_response(
    status: int, body: bytes, new_cookie: str | None = None,
) -> Response:
    response = Response(content=body, status_code=status, media_type="application/json", headers={
        "Cache-Control": "no-cache, no-store",
        "X-Content-Type-Options": "nosniff",
    })
    if new_cookie:
        response.set_cookie(
            COOKIE, new_cookie, httponly=True, samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )
    return response


def _runtime_post(
    session_id: str,
    request: Request,
    action: str,
    body: dict,
    timeout: int = 15,
    caller: tuple[str, str | None] | None = None,
):
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Interactive Pi backend not connected")
    if not session_id or len(session_id) > 128 or not all(c.isalnum() or c in "_-" for c in session_id):
        raise HTTPException(404, "runtime session not found")
    owner, new_cookie = caller or _runtime_caller_owner(request)
    target = (
        f"{PI_RUNTIME_BASE}/v1/sessions/{urllib.parse.quote(session_id, safe='')}/"
        f"{urllib.parse.quote(action, safe='')}"
    )
    payload = json.dumps(body, separators=(",", ":")).encode()
    upstream_request = urllib.request.Request(target, method="POST", data=payload, headers={
        "Authorization": f"Bearer {PI_RUNTIME_INTERNAL_TOKEN}",
        "X-ChemRxn-User-Id": owner,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(upstream_request, timeout=timeout) as upstream:
            response_body = upstream.read()
            status = upstream.status
    except urllib.error.HTTPError as exc:
        status = exc.code if exc.code in (400, 401, 403, 404, 409, 503) else 502
        response_body = exc.read()
        exc.close()
    except Exception:
        raise HTTPException(503, "Interactive Pi backend not connected") from None
    return _runtime_response(status, response_body, new_cookie)


# ───────────────────── distributed blind Figure shards ─────────────────────
_DISTRIBUTED_ARCHIVE_LIMIT = 80 * 1024 * 1024
_DISTRIBUTED_EXPANDED_LIMIT = 800 * 1024 * 1024
_DISTRIBUTED_ENTRY_LIMIT = 400
_DISTRIBUTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}


def _distributed_meta_path(batch_id: str) -> Path:
    return DISTRIBUTED_RUNS_DIR / batch_id / "batch.json"


def _distributed_write_meta(batch_id: str, payload: dict) -> None:
    target = _distributed_meta_path(batch_id)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, target)


def _distributed_load_meta(batch_id: str) -> dict:
    if not re.fullmatch(r"dist_[0-9a-f]{16}", batch_id or ""):
        raise HTTPException(404, "distributed shard not found")
    payload = _read_json(_distributed_meta_path(batch_id))
    if not isinstance(payload, dict):
        raise HTTPException(404, "distributed shard not found")
    return payload


def _distributed_owner(request: Request) -> tuple[str, str | None]:
    if PI_RUNTIME_INTERNAL_TOKEN:
        return _runtime_caller_owner(request)
    # The batch UI may still inspect an already staged shard while the local Pi runtime is down.
    response = Response()
    owner = _ensure_session(request, response)
    cookie = response.headers.get("set-cookie")
    token = None
    if cookie:
        match = re.search(rf"{re.escape(COOKIE)}=([^;]+)", cookie)
        token = match.group(1) if match else None
    return owner, token


def _distributed_require_owner(meta: dict, owner: str) -> None:
    if meta.get("owner") != owner:
        raise HTTPException(404, "distributed shard not found")


def _distributed_safe_extract(archive: Path, destination: Path) -> Path:
    """Extract a participant input archive without links, traversal, devices, or expansion bombs."""
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    total = 0
    files = 0
    roots = set()
    with tarfile.open(archive, "r:gz") as handle:
        members = handle.getmembers()
        if len(members) > _DISTRIBUTED_ENTRY_LIMIT:
            raise ValueError("shard archive contains too many entries")
        for member in members:
            parts = Path(member.name).parts
            if not parts or member.name.startswith("/") or ".." in parts:
                raise ValueError("shard archive contains an unsafe path")
            roots.add(parts[0])
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError("shard archive links/devices are forbidden")
            target = destination.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            if not member.isfile():
                raise ValueError("shard archive contains an unsupported entry")
            files += 1
            total += max(0, member.size)
            if total > _DISTRIBUTED_EXPANDED_LIMIT:
                raise ValueError("shard archive expands beyond the configured limit")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = handle.extractfile(member)
            if source is None:
                raise ValueError("shard archive member is unreadable")
            with target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            try:
                os.chmod(target, 0o700 if target.name == "run.sh" else 0o600)
            except OSError:
                pass
    if files < 5 or len(roots) != 1:
        raise ValueError("shard archive must contain exactly one shard root")
    shard = destination / next(iter(roots))
    if not shard.is_dir():
        raise ValueError("shard root is not a directory")
    return shard


def _distributed_validator() -> Path:
    return DISTRIBUTED_CODE_ROOT / "figure_skills" / "distributed" / "validate_distributed_shard.py"


def _distributed_validate(shard: Path, result: Path | None = None) -> dict:
    validator = _distributed_validator()
    if not validator.is_file():
        raise HTTPException(503, "Frozen distributed runner is not installed")
    command = [sys.executable, str(validator), "--shard", str(shard)]
    if result is not None:
        command.extend(["--result", str(result)])
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    try:
        payload = json.loads(completed.stdout)
    except Exception:
        payload = {"valid": False, "errors": [(completed.stderr or completed.stdout)[-2000:]]}
    if completed.returncode != 0 or payload.get("valid") is not True:
        raise HTTPException(409, {"message": "Distributed shard validation failed", "validation": payload})
    return payload


def _distributed_manifest(shard: Path) -> list[dict]:
    path = shard / "manifest.pre_reference.jsonl"
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:
        raise HTTPException(409, "Distributed shard manifest is unreadable") from None
    return rows


def _distributed_case_output_id(row: dict) -> str:
    image = Path(str(row.get("image") or ""))
    stem = image.stem
    digest = str(row.get("image_sha256") or "")[:12]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{stem}-{digest}").strip("_")


def _distributed_process_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _distributed_refresh(meta: dict) -> dict:
    batch_id = meta["id"]
    root = DISTRIBUTED_RUNS_DIR / batch_id
    shard = root / "shard"
    rows = _distributed_manifest(shard)
    result = root / "results"
    exit_path = root / "PROCESS_EXIT_CODE"
    sealed = 0
    cases = []
    for row in rows:
        output_id = _distributed_case_output_id(row)
        output = result / "cases" / output_id / "output"
        status = "sealed" if (output / "SEAL.pre_reference.json").is_file() else (
            "running" if output.parent.is_dir() else "pending"
        )
        if status == "sealed":
            sealed += 1
        cases.append({
            "id": row.get("id"),
            "source": row.get("source"),
            "difficulty": row.get("difficulty"),
            "image": row.get("image"),
            "status": status,
            "outputId": output_id,
            "reviewable": status == "sealed",
        })
    status = meta.get("status", "staged")
    exit_code = None
    if exit_path.is_file():
        try:
            exit_code = int(exit_path.read_text().strip())
        except Exception:
            exit_code = -1
        validation = _read_json(result / "VALIDATION.json") or {}
        status = "complete" if exit_code == 0 and validation.get("valid") is True else "failed"
    elif status == "running" and not _distributed_process_alive(meta.get("pid")):
        status = "interrupted"
    changed = status != meta.get("status") or sealed != meta.get("sealed")
    meta = {**meta, "status": status, "sealed": sealed, "total": len(rows), "exitCode": exit_code,
            "updated": time.time()}
    if changed:
        _distributed_write_meta(batch_id, meta)
    return {**meta, "cases": cases, "validation": _read_json(result / "VALIDATION.json")}


def _distributed_response(payload: dict, new_cookie: str | None = None, status_code: int = 200):
    response = JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})
    if new_cookie:
        response.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
    return response


@app.post("/api/distributed/shards")
async def api_distributed_upload(request: Request, archive: UploadFile = File(...)):
    """Upload and preflight one trusted coordinator-produced, GT-free shard package."""
    owner, new_cookie = _distributed_owner(request)
    filename = archive.filename or ""
    if not filename.endswith(".input.tar.gz"):
        raise HTTPException(400, "Choose a coordinator-produced *.input.tar.gz shard")
    batch_id = f"dist_{uuid.uuid4().hex[:16]}"
    root = DISTRIBUTED_RUNS_DIR / batch_id
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    archive_path = root / "input.tar.gz"
    digest = hashlib.sha256()
    size = 0
    with archive_path.open("xb") as output:
        while True:
            chunk = await archive.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > _DISTRIBUTED_ARCHIVE_LIMIT:
                raise HTTPException(413, "Shard archive exceeds 80 MB")
            digest.update(chunk)
            output.write(chunk)
    extracted = root / "extracted"
    try:
        original_shard = _distributed_safe_extract(archive_path, extracted)
    except (tarfile.TarError, ValueError, OSError) as exc:
        raise HTTPException(400, f"Unsafe or invalid shard archive: {exc}") from None
    shard = root / "shard"
    original_shard.rename(shard)
    validation = _distributed_validate(shard)
    shard_meta = _read_json(shard / "SHARD_METADATA.json") or {}
    rows = _distributed_manifest(shard)
    pin = shard_meta.get("repo_pin")
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise HTTPException(409, "Shard has no valid frozen repository pin")
    payload = {
        "id": batch_id,
        "owner": owner,
        "shardId": shard_meta.get("shard_id") or shard.name,
        "status": "staged",
        "created": time.time(),
        "updated": time.time(),
        "archiveSha256": digest.hexdigest(),
        "archiveBytes": size,
        "repoPin": pin,
        "total": len(rows),
        "sealed": 0,
        "preflight": validation,
    }
    _distributed_write_meta(batch_id, payload)
    return _distributed_response(_distributed_refresh(payload), new_cookie, 201)


@app.get("/api/distributed/shards")
def api_distributed_list(request: Request):
    owner, new_cookie = _distributed_owner(request)
    batches = []
    if DISTRIBUTED_RUNS_DIR.is_dir():
        for path in sorted(DISTRIBUTED_RUNS_DIR.glob("dist_*/batch.json")):
            payload = _read_json(path)
            if isinstance(payload, dict) and payload.get("owner") == owner:
                current = _distributed_refresh(payload)
                current.pop("cases", None)
                batches.append(current)
    batches.sort(key=lambda item: item.get("created", 0), reverse=True)
    return _distributed_response({"batches": batches}, new_cookie)


@app.get("/api/distributed/shards/{batch_id}")
def api_distributed_status(batch_id: str, request: Request):
    owner, new_cookie = _distributed_owner(request)
    meta = _distributed_load_meta(batch_id)
    _distributed_require_owner(meta, owner)
    return _distributed_response(_distributed_refresh(meta), new_cookie)


@app.post("/api/distributed/shards/{batch_id}/start")
async def api_distributed_start(batch_id: str, request: Request):
    owner, new_cookie = _distributed_owner(request)
    meta = _distributed_load_meta(batch_id)
    _distributed_require_owner(meta, owner)
    current = _distributed_refresh(meta)
    if current.get("status") != "staged":
        raise HTTPException(409, f"Shard is already {current.get('status')}")
    try:
        body = await request.json()
    except Exception:
        body = {}
    concurrency = body.get("concurrency", 1) if isinstance(body, dict) else 1
    if concurrency not in (1, 2):
        raise HTTPException(400, "concurrency must be 1 or 2")
    if not PI_RUNTIME_INTERNAL_TOKEN:
        raise HTTPException(503, "Local Pi runtime is not connected")
    auth_status, auth_body, _ = await run_in_threadpool(
        _runtime_json, "GET", "/v1/auth/status", owner, None, 10,
    )
    try:
        auth_payload = json.loads(auth_body)
    except Exception:
        auth_payload = {}
    if auth_status >= 400 or auth_payload.get("configured") is not True:
        raise HTTPException(409, "Run `./chemrxn login` before starting this shard")
    try:
        actual_pin = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=DISTRIBUTED_CODE_ROOT,
            text=True, timeout=10, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        raise HTTPException(503, "Frozen distributed code checkout is unavailable") from None
    if actual_pin != meta.get("repoPin"):
        raise HTTPException(409, f"Shard requires code {meta.get('repoPin')}; configured checkout is {actual_pin}")
    runner = DISTRIBUTED_CODE_ROOT / "figure_skills" / "distributed" / "run_distributed_shard.sh"
    if not runner.is_file() or not DISTRIBUTED_PI_AGENT_DIR.is_dir():
        raise HTTPException(503, "Distributed runner or isolated Pi profile is unavailable")
    root = DISTRIBUTED_RUNS_DIR / batch_id
    log = (root / "run.log").open("ab", buffering=0)
    exit_path = root / "PROCESS_EXIT_CODE"
    command = [
        "bash", "-c",
        '"$1" "$2" --out "$3" --concurrency "$4"; code=$?; printf "%s\\n" "$code" >"$5"; exit "$code"',
        "distributed-runner", str(runner), str(root / "shard"), str(root / "results"),
        str(concurrency), str(exit_path),
    ]
    env = os.environ.copy()
    env.update({
        "CHEMRXN_REPO": str(DISTRIBUTED_CODE_ROOT),
        "PI_CODING_AGENT_DIR": str(DISTRIBUTED_PI_AGENT_DIR),
        "RXN_SKILLS_PYTHON": sys.executable,
        "PATH": f"{DISTRIBUTED_CODE_ROOT / 'node_modules' / '.bin'}:{env.get('PATH', '')}",
    })
    process = subprocess.Popen(
        command, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True, close_fds=True,
    )
    log.close()
    DISTRIBUTED_PROCESSES[batch_id] = process
    meta = {**meta, "status": "running", "pid": process.pid, "concurrency": concurrency,
            "started": time.time(), "updated": time.time()}
    _distributed_write_meta(batch_id, meta)
    return _distributed_response(_distributed_refresh(meta), new_cookie, 202)


def _distributed_register_case(meta: dict, case_id: str, owner: str) -> dict:
    root = DISTRIBUTED_RUNS_DIR / meta["id"]
    shard = root / "shard"
    rows = {str(row.get("id")): row for row in _distributed_manifest(shard)}
    row = rows.get(case_id)
    if row is None:
        raise HTTPException(404, "distributed case not found")
    output_id = _distributed_case_output_id(row)
    output = root / "results" / "cases" / output_id / "output"
    prediction_path = output / "pred.v2.json"
    if not (prediction_path.is_file() and (output / "SEAL.pre_reference.json").is_file()):
        raise HTTPException(409, "Case is not sealed yet")
    prediction_bytes = prediction_path.read_bytes()
    try:
        prediction = json.loads(prediction_bytes)
    except Exception:
        raise HTTPException(409, "Sealed prediction is invalid JSON") from None
    image = shard / str(row["image"])
    cache = shard / str(row["cache"])
    if hashlib.sha256(image.read_bytes()).hexdigest() != row.get("image_sha256"):
        raise HTTPException(409, "Distributed source hash drift")
    if hashlib.sha256(cache.read_bytes()).hexdigest() != row.get("cache_sha256"):
        raise HTTPException(409, "Distributed MolParser hash drift")
    job_id = f"dist_{hashlib.sha256((meta['id'] + ':' + case_id).encode()).hexdigest()[:16]}"
    slug, tid = f"upload_{job_id}", "0"
    jdir, work, out_dir = JOBS_DIR / job_id, JOBS_DIR / job_id / "work", PDF_ROOT / slug
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    source_path = jdir / f"input{image.suffix.lower()}"
    shutil.copyfile(image, source_path)
    _to_png(source_path, work / "0.png")
    shutil.copyfile(work / "0.png", out_dir / "0.png")
    (jdir / "result.json").write_bytes(prediction_bytes)
    shutil.copyfile(cache, work / "0_molparser.json")
    shutil.copyfile(cache, out_dir / "0_molparser.json")
    normalized = _normalize_v2(copy.deepcopy(prediction), slug, tid)
    normalized["extractor"] = "Pi reaction-extract · distributed sealed shard"
    (out_dir / "0_predicted_output.json").write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (out_dir / "reactions.json").write_text(json.dumps({
        "paper_id": slug, "title": f"{meta.get('shardId')} · {case_id}", "page_count": 1,
        "extractor": normalized["extractor"], "source_prediction_sha256": hashlib.sha256(prediction_bytes).hexdigest(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    now = time.time()
    JOBS[job_id] = {
        "id": job_id, "status": "done", "phase": "done", "filename": image.name,
        "mode": "distributed", "created": now, "started": now, "ended": now, "owner": owner,
        "provider": "openai-codex", "model_name": "gpt-5.6-sol", "valid": True,
        "n_examples": len(prediction.get("selected_examples") or []),
        "n_detections": len(json.loads(cache.read_text(encoding="utf-8"))),
        "reviewer_url": f"{REVIEWER_BASE}/#/{slug}/0", "distributed_batch": meta["id"],
        "distributed_case": case_id,
    }
    _save_meta(job_id)
    return {"jobId": job_id, "workspaceUrl": f"/?job={urllib.parse.quote(job_id)}",
            "reviewerUrl": JOBS[job_id]["reviewer_url"]}


@app.get("/api/distributed/shards/{batch_id}/submission")
def api_distributed_submission(batch_id: str, request: Request, checksum: bool = False):
    owner, _ = _distributed_owner(request)
    meta = _distributed_load_meta(batch_id)
    _distributed_require_owner(meta, owner)
    current = _distributed_refresh(meta)
    if current.get("status") != "complete":
        raise HTTPException(409, "A validated submission is not ready")
    shard = DISTRIBUTED_RUNS_DIR / batch_id / "shard"
    suffix = ".submission.tar.gz.sha256" if checksum else ".submission.tar.gz"
    files = sorted(path for path in shard.glob(f"*{suffix}") if path.is_file())
    if len(files) != 1:
        raise HTTPException(409, "Validated submission artifact is missing")
    return FileResponse(
        files[0], filename=files[0].name,
        media_type="text/plain" if checksum else "application/gzip",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/distributed/shards/{batch_id}/cases/{case_id}/reviewer")
def api_distributed_case_reviewer(batch_id: str, case_id: str, request: Request):
    owner, new_cookie = _distributed_owner(request)
    meta = _distributed_load_meta(batch_id)
    _distributed_require_owner(meta, owner)
    payload = _distributed_register_case(meta, case_id, owner)
    return _distributed_response(payload, new_cookie)


ALLOWED_PROVIDERS = ("gemini-vertex", "gemini-api", "openai", "anthropic")   # legacy raw backend names (BYO)
# Default model per BYO provider when the form leaves it blank. We DON'T hard-restrict the model to an allow-list:
# it's the user's own key/account, so they may have access to models we didn't enumerate. We only sanitize it.
DEFAULT_BYO_MODEL = {"gemini-api": "gemini-2.5-pro", "openai": "gpt-4o", "anthropic": "claude-sonnet-4-5"}


def _model_access(provider: str):
    """(access_tier, spec) for a provider/model id via model_registry; falls back to byo for raw backend names."""
    try:
        from model_registry import get_spec, access_of
        return access_of(provider), get_spec(provider)
    except Exception:
        return "byo", None


def _clean_model(m: str) -> str:
    """Accept any plausible model identifier (incl. gateway ids with '/' like 'Vendor2/Gemini-2.5-Pro' and vLLM
    names like 'Qwen3.5-397B-A17B-FP8'), but reject junk/injection. Empty -> caller default."""
    m = (m or "").strip()
    return m if (0 < len(m) <= 80 and all(c.isalnum() or c in ".-_:/" for c in m)) else ""


def _resolve_llm(request: Request, llm_provider: str, llm_key: str, llm_model: str,
                 llm_base_url: str = "", llm_endpoint: str = "", llm_api_version: str = ""):
    """Resolve (provider, api_key, model_name, conn) for a job by the model's ACCESS tier (model_registry):
      - public     (local vLLM)              -> anyone; key/base_url come from the registry/.env (api_key None).
      - privileged (managed gemini/azure/geek)-> requires _is_privileged (admin or chemagent-unlocked) else 403;
                                                key from our .env (api_key None).
      - byo        (user's own)              -> key from the form/session vault (else 400) + optional base_url/
                                                azure endpoint+version for an openai-compatible/azure endpoint.
    `conn` = {base_url, azure_endpoint, api_version} for BYO custom endpoints. The returned key is handed straight
    to the worker stack -- never persisted or logged."""
    known = set(getattr(__import__("model_registry"), "MODELS", {}))
    provider = llm_provider if (llm_provider in known or llm_provider in ALLOWED_PROVIDERS) else "gemini-vertex"
    access, _spec = _model_access(provider)
    model = _clean_model(llm_model) or None          # None -> make_backend uses the registry spec's default
    # Managed (public/privileged) providers with a curated sub-model list: reject an off-list model name
    # (anti-injection — it's OUR paid key) and fall back to the spec default. `open_models` providers (geek, whose
    # gateway carries ~114 ids the user searches) skip this. BYO stays unrestricted (user's key).
    if access in ("public", "privileged") and _spec and _spec.get("models") and model and not _spec.get("open_models"):
        if model not in {m.get("id") for m in _spec["models"]}:
            model = None
    if access == "public":
        return provider, None, model, {}
    if access == "privileged":
        if not _is_privileged(request):
            raise HTTPException(403, "this model is a paid model — log in as admin or unlock with the chemagent access key")
        return provider, None, model, {}
    # byo
    key = (llm_key or "").strip() or _vault_get_key(_vault_sid(request))
    if not key:
        raise HTTPException(400, "bring-your-own model needs an API key (paste it, or save one this session)")
    if not model:
        model = DEFAULT_BYO_MODEL.get(_spec.get("backend") if _spec else provider, "gpt-4o")
    conn = {}
    if (llm_base_url or "").strip():
        conn["base_url"] = llm_base_url.strip()
    if (llm_endpoint or "").strip():
        conn["azure_endpoint"] = llm_endpoint.strip()
    if (llm_api_version or "").strip():
        conn["api_version"] = llm_api_version.strip()
    return provider, key, model, conn


@app.post("/api/unlock")
async def api_unlock(request: Request):
    """Unlock the PAID models for this session by submitting the chemagent access key. Constant-time compare;
    the key is never echoed. On success the session is marked privileged (managed Gemini / Azure / geek become
    selectable). Admin accounts (demo) are privileged already and don't need this."""
    body = await request.json()
    supplied = (body.get("key") or "").strip()
    secret = _chemagent_key()
    import hmac
    if not secret or not supplied or not hmac.compare_digest(supplied, secret):
        raise HTTPException(401, "invalid access key")
    resp = JSONResponse({"ok": True, "privileged": True})
    tok = request.cookies.get(COOKIE)
    if not tok or tok not in SESSIONS:
        tok = _new_session(None)
        resp.set_cookie(COOKIE, tok, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    SESSIONS[tok]["privileged"] = True
    _auth_save(SESSIONS_FILE, SESSIONS)
    return resp


@app.get("/api/models")
def api_models(request: Request):
    """Model picker data for the caller: every model's id/label/access/backend + whether the caller may use it
    (public always; privileged only if admin/unlocked; byo always but needs a key). No secrets."""
    from model_registry import public_list
    priv = _is_privileged(request)
    out = []
    for m in public_list():
        usable = (m["access"] == "public") or (m["access"] == "byo") or (m["access"] == "privileged" and priv)
        out.append({**m, "usable": usable, "locked": (m["access"] == "privileged" and not priv)})
    return {"models": out, "privileged": priv, "identity": _identity(request) or None}


_PROVIDER_MODELS_CACHE = {}    # provider_id -> (ts, [{id,label,vision}]); ~1h TTL for live gateway lists


@app.get("/api/provider_models/{provider_id}")
def api_provider_models(provider_id: str, request: Request):
    """Sub-model list for the picker's searchable box. For an `open_models` gateway provider (geek) fetch the LIVE
    gateway /v1/models (cached ~1h, proxy-bypassed, key from .env and NEVER echoed); otherwise return the spec's
    curated models. Privileged providers require auth; any fetch failure falls back to the spec list (never 500)."""
    from model_registry import get_spec, _env
    spec = get_spec(provider_id)
    if not spec:
        return {"models": []}
    if spec.get("access") == "privileged" and not _is_privileged(request):
        raise HTTPException(403, "paid model — unlock with the chemagent key or log in as admin")
    spec_models = [{"id": m.get("id"), "label": m.get("label", m.get("id")), "vision": bool(m.get("vision", True))}
                   for m in (spec.get("models") or []) if m.get("id")]
    if not (spec.get("open_models") and spec.get("base_url")):
        return {"models": spec_models}
    cached = _PROVIDER_MODELS_CACHE.get(provider_id)
    if cached and time.time() - cached[0] < 3600:
        return {"models": cached[1] or spec_models}
    live = []
    try:
        import urllib.request
        key = _env(spec.get("key_env"))
        req = urllib.request.Request(spec["base_url"].rstrip("/") + "/models",
                                     headers={"Authorization": "Bearer " + (key or "")})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # bypass server proxy (geek no_proxy)
        with opener.open(req, timeout=15) as r:
            data = json.loads(r.read())
        for m in (data.get("data") or []):
            mid = m.get("id")
            if mid:
                live.append({"id": mid, "label": mid, "vision": True})
    except Exception:
        live = []
    merged = live or spec_models
    _PROVIDER_MODELS_CACHE[provider_id] = (time.time(), merged)
    return {"models": merged}


@app.get("/api/llm_key")
def api_llm_key_get(request: Request):
    """Whether this session has a saved BYO key (+ which provider/model). NEVER returns the key itself."""
    return _vault_get_meta(_vault_sid(request))


@app.post("/api/llm_key")
async def api_llm_key_set(request: Request):
    """Save the caller's BYO key for THIS session, Fernet-encrypted. Mints a session cookie if they have none."""
    body = await request.json()
    provider = body.get("provider") or "gemini-api"
    if provider not in ALLOWED_PROVIDERS or provider == "gemini-vertex":
        raise HTTPException(400, "only bring-your-own providers can be saved")
    model = _clean_model(body.get("model")) or DEFAULT_BYO_MODEL.get(provider, "gpt-4o")
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "no key")
    sid = _vault_sid(request)
    new_cookie = None
    if not sid:
        sid = _new_session(None)
        new_cookie = sid
    ok = _vault_set(sid, provider, model, key)
    out = JSONResponse({"saved": ok, "provider": provider, "model": model})
    if new_cookie:
        out.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return out


@app.delete("/api/llm_key")
def api_llm_key_clear(request: Request):
    """Forget the session's saved BYO key."""
    _vault_clear(_vault_sid(request))
    return {"cleared": True}


@app.post("/api/extract")
async def api_extract(request: Request, file: UploadFile = File(...), mode: str = Form("fast"),
                      llm_provider: str = Form("gemini-vertex"), llm_key: str = Form(""),
                      llm_model: str = Form(""), tag: str = Form(""),
                      llm_base_url: str = Form(""), llm_endpoint: str = Form(""),
                      llm_api_version: str = Form(""),
                      no_molparser: str = Form(""), grid_big: str = Form(""), understand_first: str = Form(""),
                      images_mode: str = Form("full")):
    ext = Path(file.filename or "upload").suffix.lower()
    if ext not in IMG_EXT:
        raise HTTPException(400, f"upload an image ({sorted(IMG_EXT)}); got '{ext}'")
    mode = mode if mode in ("fast", "precision") else "fast"   # High-Precision = sandbox + semantic verify
    # Cap the upload: read at most MAX_UPLOAD_BYTES+1 so a huge file can't OOM the single-process server.
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"image too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    if not data:
        raise HTTPException(400, "empty upload")
    provider, api_key, model_name, conn = _resolve_llm(request, llm_provider, llm_key, llm_model,
                                                       llm_base_url, llm_endpoint, llm_api_version)
    # EXPERIMENT flags (non-secret, behind-default): threaded via conn to the worker->run, never change default
    # behavior unless explicitly set. no_molparser=pure vision; grid_big=larger compare crops; understand_first=comprehension pass.
    _truthy = lambda s: str(s).strip().lower() in ("1", "true", "yes", "on")
    for _k, _v in (("no_molparser", no_molparser), ("grid_big", grid_big), ("understand_first", understand_first)):
        if _truthy(_v):
            conn[_k] = True
    if str(images_mode).strip().lower() in ("lean", "progressive"):   # A/B/C image-context experiment (default full)
        conn["images_mode"] = str(images_mode).strip().lower()
    owner, new_cookie = _caller_owner(request)
    job_id = _queue_job(data, ext, file.filename, mode, owner, provider, api_key, model_name, tag=tag, conn=conn)
    out = JSONResponse({"job_id": job_id, "status": "queued", "mode": mode, "provider": provider})
    if new_cookie:
        out.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return out


@app.post("/api/extract_batch")
async def api_extract_batch(request: Request, files: list[UploadFile] = File(...), mode: str = Form("fast"),
                            llm_provider: str = Form("gemini-vertex"), llm_key: str = Form(""),
                            llm_model: str = Form("")):
    """Batch upload: each image becomes its own owned job, queued through the worker pool (the queue-position
    UX shows where each sits). Returns one row per file (job_id, or an error for files that were rejected)."""
    mode = mode if mode in ("fast", "precision") else "fast"
    if not files:
        raise HTTPException(400, "no files")
    if len(files) > MAX_BATCH:
        raise HTTPException(413, f"too many files (max {MAX_BATCH} per batch)")
    provider, api_key, model_name, conn = _resolve_llm(request, llm_provider, llm_key, llm_model)  # one LLM for the batch
    owner, new_cookie = _caller_owner(request)
    rows = []
    for f in files:
        ext = Path(f.filename or "upload").suffix.lower()
        if ext not in IMG_EXT:
            rows.append({"filename": f.filename, "error": f"not an image ({ext})"}); continue
        data = await f.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            rows.append({"filename": f.filename, "error": "too large"}); continue
        if not data:
            rows.append({"filename": f.filename, "error": "empty"}); continue
        jid = _queue_job(data, ext, f.filename, mode, owner, provider, api_key, model_name, conn=conn)
        rows.append({"filename": f.filename, "job_id": jid, "status": "queued"})
    out = JSONResponse({"jobs": rows, "queued": sum(1 for r in rows if r.get("job_id")), "total": len(rows)})
    if new_cookie:
        out.set_cookie(COOKIE, new_cookie, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return out


STALE_RUNNING_SECS = 8 * 60   # a "running" job whose transcript hasn't moved this long is a killed run, not live

def _destale_status(jdir: Path, status):
    """A killed/interrupted run leaves meta status='running' forever; the UI then auto-follows it and
    shows stale data. If the transcript hasn't been written in STALE_RUNNING_SECS, report 'incomplete'."""
    if status == "running":
        tj = jdir / "transcript.json"
        mt = tj.stat().st_mtime if tj.exists() else 0
        if not mt or time.time() - mt > STALE_RUNNING_SECS:
            return "incomplete"
    return status


def _mark_orphans_on_boot():
    """A restart loses every in-flight thread + the in-memory JOBS dict. Any on-disk job still marked
    running/queued is orphaned (its worker is gone, its tokens already spent). Mark it terminal ('incomplete',
    a status the UI already treats as done) immediately, instead of leaving a stale 'running' that the UI
    auto-follows for 8 minutes (STALE_RUNNING_SECS)."""
    if not JOBS_DIR.exists():
        return
    n = 0
    for d in JOBS_DIR.iterdir():
        if not d.is_dir():
            continue
        m = _read_json(d / "meta.json")
        if m and m.get("status") in ("running", "queued"):
            m["status"], m["reason"] = "incomplete", "interrupted (server restarted)"
            try:
                tmp = d / "meta.json.tmp"; tmp.write_text(json.dumps(m)); os.replace(tmp, d / "meta.json"); n += 1
            except Exception:
                pass
    if n:
        print(f"[harness_server] marked {n} orphaned running/queued job(s) interrupted on boot", flush=True)


def _is_internal_job(m: dict) -> bool:
    """Internal eval/benchmark mirrors — SkillOpt-evolve rollouts (id 'evo-...', filename 'X - evolve <run>')
    and benchmark registrations ('NN/N (benchmark)') — must NOT clutter the user-facing product history."""
    jid = str(m.get("id") or "")
    fn = str(m.get("filename") or "")
    note = str(m.get("note") or "").lower()
    return (jid.startswith("evo-") or " - evolve " in fn or "(benchmark)" in fn
            or "evolve" in note or "skillopt" in note)


@app.get("/api/jobs")
def api_jobs(request: Request, limit: int = 100, offset: int = 0, include_internal: bool = False, all: bool = False):
    """List the CALLER'S past uploads (newest first). Each job is owner-tagged (logged-in user or guest session),
    so a user sees only their own — the multi-tenant history. Internal eval/benchmark mirrors are always hidden;
    legacy pre-accounts jobs (no owner) are hidden from the per-user view (pass all=1 to see every non-internal
    job, for demo/admin). Paginated: returns {jobs, total, offset, limit, identity}."""
    ident = _identity(request)
    out = []
    if JOBS_DIR.exists():
        for d in JOBS_DIR.iterdir():
            if not d.is_dir():
                continue
            fig = d / "work" / "0.png"
            m = _read_json(d / "meta.json")
            if not m:                                  # synthesize for jobs created before meta.json existed
                if not fig.exists():
                    continue
                res = _read_json(d / "result.json")
                m = {"id": d.name, "filename": d.name,
                     "status": "done" if res else "incomplete",
                     "n_examples": len(res.get("selected_examples") or []) if res else None,
                     "created": fig.stat().st_mtime}
            if not include_internal and _is_internal_job(m):
                continue
            if not all and m.get("owner") != ident:    # per-user isolation: only the caller's own jobs
                continue
            recorded_owner = m.get("owner")
            can_access = not recorded_owner or recorded_owner in {
                value for value in (ident, LOCAL_RUNTIME_USER_ID) if value
            }
            m["has_figure"] = fig.exists()
            m["has_overlay"] = (d / "work" / "0_annotated.png").exists()
            m["status"] = _destale_status(d, m.get("status"))
            m["source_rights"] = _job_source_rights(d.name, m)
            m["source_rights_status"] = _job_source_rights_status(d.name, m)
            m["can_access"] = can_access
            # The all-jobs showcase may include other users' metadata, but
            # never disclose their stable owner identity.
            m.pop("owner", None)
            for key in _SOURCE_RIGHTS_FIELDS:
                m.pop(key, None)
            out.append(m)
    out.sort(key=lambda x: x.get("created") or 0, reverse=True)
    total = len(out)
    page = out[max(0, offset): max(0, offset) + max(1, min(limit, 500))]
    return {"jobs": page, "total": total, "offset": offset, "limit": limit, "identity": ident or None}


@app.get("/api/usage")
def api_usage(request: Request):
    """Account-level totals for the caller (logged-in user or guest session): extractions, tokens, and spend,
    split by provider. Managed (our Vertex) cost is real $ on OUR key. BYO costs are the user's own and priced by
    THEIR provider; cost_usd was computed with managed-Gemini pricing so it's only meaningful for gemini-vertex --
    we therefore report BYO token counts but not a dollar figure. Same owner filter as /api/jobs (no pagination)."""
    ident = _identity(request)
    jobs = ti = to = tc = 0
    managed_cost = 0.0
    by: dict = {}
    if JOBS_DIR.exists():
        for d in JOBS_DIR.iterdir():
            if not d.is_dir():
                continue
            m = _read_json(d / "meta.json")
            if not m or _is_internal_job(m) or m.get("owner") != ident:
                continue
            jobs += 1
            ti += m.get("tokens_in") or 0
            to += m.get("tokens_out") or 0
            tc += m.get("tokens_cached") or 0
            p = m.get("provider") or "gemini-vertex"
            b = by.setdefault(p, {"jobs": 0, "tokens": 0, "cost_usd": None})
            b["jobs"] += 1
            b["tokens"] += (m.get("tokens_in") or 0) + (m.get("tokens_out") or 0)
            if p == "gemini-vertex":                       # only managed cost is a real, correctly-priced $ figure
                c = m.get("cost_usd") or 0.0
                managed_cost += c
                b["cost_usd"] = round((b["cost_usd"] or 0.0) + c, 4)
    return {"identity": ident or None, "jobs": jobs, "tokens": ti + to,
            "tokens_in": ti, "tokens_out": to, "tokens_cached": tc,
            "managed_cost_usd": round(managed_cost, 4), "by_provider": by}


_COLLECTIONS_DIR = JOBS_DIR / "_collections"


def _collection_path(name: str) -> Path:
    """Safe path to a collection index file (no traversal)."""
    safe = "".join(c for c in (name or "") if c.isalnum() or c in "-_.")
    return _COLLECTIONS_DIR / f"{safe}.jsonl"


@app.get("/api/collections")
def api_collections():
    """List browsable job COLLECTIONS (folders) from <JOBS_DIR>/_collections/*.jsonl, each a compact card index
    written by scripts/register_live_jobs.py. Returns [{name, count}] cheaply (no scan of thousands of meta.json),
    so the UI can show folders for synfact / direct / rxncaption / ... without loading every job."""
    out = []
    if _COLLECTIONS_DIR.exists():
        for f in sorted(_COLLECTIONS_DIR.glob("*.jsonl")):
            try:
                count = sum(1 for ln in f.read_text().splitlines() if ln.strip())
            except Exception:
                count = 0
            out.append({"name": f.stem, "count": count})
    return {"collections": out}


@app.get("/api/collections/{name}")
def api_collection(name: str, offset: int = 0, limit: int = 50, model: str = None):
    """One PAGE of a collection's compact cards (newest-first), so the UI renders only the visible page into the
    DOM — never the whole (possibly 10k-fig) collection. Optional `model` filters by backbone (gemini / qwen /
    gpt-5-mini / bare-* / chemeagle) so the same source folder can be sliced by which model produced each result.
    `models` returns the backbone breakdown (for the filter chips). Card detail loads on click via /api/jobs/{id}/*."""
    path = _collection_path(name)
    if not path.exists():
        raise HTTPException(404, "unknown collection")
    cards = []
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            cards.append(json.loads(ln))
        except Exception:
            pass
    models: dict = {}
    for c in cards:
        b = c.get("backbone") or c.get("provider") or "?"
        models[b] = models.get(b, 0) + 1
    if model:
        cards = [c for c in cards if (c.get("backbone") or c.get("provider")) == model]
    cards.sort(key=lambda c: c.get("created") or 0, reverse=True)
    total = len(cards)
    lo = max(0, offset)
    page = cards[lo: lo + max(1, min(limit, 200))]
    projected_page = []
    for card in page:
        card_id = str(card.get("id") or "")
        card_meta = _read_json(JOBS_DIR / card_id / "meta.json") or card
        projected = {
            key: value
            for key, value in card.items()
            if key not in _SOURCE_RIGHTS_FIELDS
        }
        projected["source_rights"] = _job_source_rights(card_id, card_meta)
        projected["source_rights_status"] = _job_source_rights_status(card_id, card_meta)
        projected_page.append(projected)
    page = projected_page
    return {"name": name, "total": total, "offset": offset, "limit": limit, "model": model,
            "models": [{"name": k, "count": v} for k, v in sorted(models.items(), key=lambda kv: -kv[1])],
            "cards": page}


def _reviewer_url_for(job_id: str):
    slug = f"upload_{job_id}"
    return f"{REVIEWER_BASE}/#/{slug}/0" if (PDF_ROOT / slug).exists() else None


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if job:
        pub = {k: job.get(k) for k in _JOB_PUBLIC}
        persisted_meta = _read_json(JOBS_DIR / job_id / "meta.json") or job
        pub["source_rights"] = _job_source_rights(job_id, persisted_meta)
        pub["source_rights_status"] = _job_source_rights_status(job_id, persisted_meta)
        if job.get("status") == "queued":
            pub["queue_position"] = _queue_position(job_id)
            pub["queue_workers"] = getattr(_pool, "_max_workers", 2)
        return pub
    # not in memory -> synthesize a finished record from disk (so reopening a past job works)
    jdir = JOBS_DIR / job_id
    meta = _read_json(jdir / "meta.json") or {}
    res = _read_json(jdir / "result.json")
    if not meta and res is None and not (jdir / "work" / "0.png").exists():
        raise HTTPException(404, "unknown job")
    rec = dict(meta)
    rec.setdefault("id", job_id)
    rec["status"] = _destale_status(jdir, meta.get("status") or ("done" if res else "incomplete"))
    rec["phase"] = meta.get("phase") or "done"          # keep "agent" for a still-running job -> UI keeps polling
    rec["reviewer_url"] = _reviewer_url_for(job_id)
    if res is not None and rec["status"] != "running" and meta.get("mode") != "pdf":
        # a document job's result.json is the paper-level pred (anchors[]); its reaction count
        # was computed at registration and lives in meta — don't zero it here
        rec["n_examples"] = len(res.get("selected_examples") or [])
    public = {k: rec.get(k) for k in _JOB_PUBLIC}
    public["source_rights"] = _job_source_rights(job_id, meta)
    public["source_rights_status"] = _job_source_rights_status(job_id, meta)
    return public


@app.post("/api/jobs/{job_id}/stop")
def api_stop(job_id: str):
    """Cancel a running extraction. Sets the job's cancel flag; the agent loop checks it at the top
    of each iteration and ends gracefully, keeping whatever partial V2 was already emitted."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not in memory (already finished or never ran here)")
    if job.get("status") in ("done", "incomplete", "error"):
        return {"job_id": job_id, "status": job.get("status"), "stopped": False, "note": "already finished"}
    job["cancel"] = True
    return {"job_id": job_id, "status": job.get("status"), "stopped": True}


@app.post("/api/jobs/{job_id}/retry")
def api_retry(job_id: str, request: Request, mode: str = None):
    """Re-run a job from its SAVED input image (a new job, same owner). Used two ways: (1) recover an
    interrupted/incomplete/error job (a dropped tunnel / timeout / stop no longer means re-uploading);
    (2) re-run a SUCCESSFUL Fast job in High-Precision via `?mode=precision`. An explicit `mode` query
    overrides the original job's mode; otherwise the original mode is honored. (True conversation-resume —
    replaying the agent's chat history to continue mid-run — is the harder follow-up.)"""
    src = JOBS_DIR / job_id
    inputs = sorted(src.glob("input.*"))
    if not inputs:
        raise HTTPException(404, "original upload not found (cannot retry)")
    meta = _read_json(src / "meta.json") or {}
    ident = _identity(request)
    if meta.get("owner") and meta.get("owner") != ident and not ident.startswith("guest:"):
        raise HTTPException(403, "not your job")
    req_mode = mode                                            # optional caller override (e.g. "re-run in HP")
    mode = meta.get("mode") if meta.get("mode") in ("fast", "precision") else "fast"
    if req_mode in ("fast", "precision"):
        mode = req_mode
    owner = meta.get("owner") or ident or "anon"
    # Honor the original job's model. A BYO job stored NO key (by design), so re-derive it from the caller's
    # per-session vault; if it's gone, ask them to re-save rather than silently bill our managed key.
    provider = meta.get("provider") if meta.get("provider") in ALLOWED_PROVIDERS else "gemini-vertex"
    model_name = meta.get("model_name")
    api_key = None
    if provider == "gemini-api":
        api_key = _vault_get_key(_vault_sid(request))
        if not api_key:
            raise HTTPException(400, "this was a bring-your-own-key job; save your Gemini key this session to retry it")
    new_id = uuid.uuid4().hex[:12]
    ndir = JOBS_DIR / new_id
    ndir.mkdir(parents=True, exist_ok=True)
    new_input = ndir / inputs[0].name
    shutil.copyfile(inputs[0], new_input)
    JOBS[new_id] = {"id": new_id, "status": "queued", "phase": "queued", "cancel": False,
                    "filename": meta.get("filename") or new_id, "mode": mode, "created": time.time(),
                    "owner": owner, "note": f"retry of {job_id}", "provider": provider, "model_name": model_name}
    _save_meta(new_id)
    _pool.submit(_worker, new_id, new_input, meta.get("filename") or new_id, mode, provider, api_key, model_name)
    return {"job_id": new_id, "status": "queued", "mode": mode, "retry_of": job_id}


@app.get("/api/jobs/{job_id}/transcript")
def api_transcript(job_id: str):
    job = JOBS.get(job_id)
    if job:
        return {"steps": job.get("transcript", []), "status": job.get("status"), "phase": job.get("phase")}
    tr = _read_json(JOBS_DIR / job_id / "transcript.json") or []   # past job: from disk
    return {"steps": tr, "status": "done", "phase": "done"}


@app.get("/api/jobs/{job_id}/figure")
def api_figure(job_id: str):
    p = JOBS_DIR / job_id / "work" / "0.png"
    if not p.exists():
        raise HTTPException(404, "no figure")
    return FileResponse(str(p))


@app.get("/api/jobs/{job_id}/overlay")
def api_overlay(job_id: str):
    """The labeled detection overview (boxes + index numbers) — same image the agent sees."""
    p = JOBS_DIR / job_id / "work" / "0_annotated.png"
    if not p.exists():
        raise HTTPException(404, "no overlay yet")
    return FileResponse(str(p))


@app.get("/api/jobs/{job_id}/result")
def api_result(job_id: str):
    """The extracted V2 (read from disk so it survives restarts and reflects partial drafts)."""
    data = _read_json(JOBS_DIR / job_id / "result.json")
    if data is None:
        raise HTTPException(404, "no result yet")
    return data


# GT V2 schema key order (matches pdf_benchmark/<p>/<n>_predicted_output.reviewed.json), so the
# export reads top-down like the schema in skills/legacy_agent_skills/02_v2_schema.md
_GT_TOP_ORDER = ["task_id", "paper_id", "anchor_image", "anchor_figure", "anchor_page",
                 "anchor_bbox_pdf", "has_reaction_template", "molparser_detection_count",
                 "extractor", "identifier_map", "is_multistep_synthesis", "synthesis_overview",
                 "reaction_templates", "selected_examples", "reaction_graph", "texts", "_opus_notes"]
_GRP_ORDER = ["template_id", "instance_id", "identifier", "inherit_template", "description",
              "markush_enumeration", "failed", "reactants", "products", "conditions", "metrics"]
_MOL_ORDER = ["identifier", "E-SMILES", "smiles", "role", "description", "markush", "_source"]


def _assert_gt_free(value, path="$"):
    """Reject reference-bearing prediction payloads before any product export.

    The sentinel remains a test-of-tests, while this structural guard catches
    ordinary GT/compare fields that do not contain the sentinel text.
    """
    if isinstance(value, dict):
        for key, nested in value.items():
            snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key).strip())
            normalized = snake.lower().replace("-", "_")
            tokens = {token for token in normalized.split("_") if token}
            if (
                "gt" in tokens
                or "gold" in tokens
                or "reference" in tokens
                or "groundtruth" in normalized.replace("_", "")
                or "compare" in tokens
                or "comparison" in tokens
                or normalized == "score_v2"
            ):
                raise HTTPException(409, f"prediction export rejected: reference field at {path}.{key}")
            _assert_gt_free(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_gt_free(nested, f"{path}[{index}]")


def _reorder(d, order):
    if not isinstance(d, dict):
        return d
    return {**{k: d[k] for k in order if k in d}, **{k: v for k, v in d.items() if k not in order}}


_METRIC_VALUE_KEYS = ("yield", "ee", "dr", "er", "rr", "conversion", "selectivity")


def _gt_metrics(m: dict) -> list:
    """Normalize ONE raw metric entry -> a LIST of GT-shape {role, of, text}. A single entry may carry
    MULTIPLE values (the model crams {yield,ee,dr,for_product} into one dict) -> expand to one metric
    per value so ee/dr aren't dropped. Already-structured {role/type, text/content} -> single entry.
    Shapes seen: {type,content,for_product}, {name,value,for_product}, {yield:'46%',ee:'90%',...}."""
    if not isinstance(m, dict):
        return [{"role": "yield", "of": None, "text": str(m)}]
    of = m.get("of") or m.get("for_product") or m.get("for") or m.get("product")
    of = str(of) if of is not None else None
    vkeys = [k for k in _METRIC_VALUE_KEYS if k in m and not isinstance(m[k], (dict, list)) and m[k] not in (None, "")]
    if vkeys:                                          # value-key dict (possibly multi) -> split
        return [{"role": k, "of": of, "text": str(m[k])} for k in vkeys]
    role = m.get("role") or m.get("type") or m.get("name")
    text = m.get("text") or m.get("content") or m.get("value")
    out = {"role": role or "yield", "of": of, "text": ("" if text is None else str(text))}
    if isinstance(m.get("_source"), dict):
        out["_source"] = m["_source"]
    return [out]


def _to_gt_v2(v2: dict, job_id: str, dets: list) -> dict:
    """Convert our emitted V2 into the GT V2 schema (02_v2_schema.md) so the downloaded file is a
    drop-in `*_predicted_output.json` for the chem_reviewer / Ketcher pipeline: manifest fields,
    identifier_map (bbox_index -> identifier), GT-shape metrics, GT key order. Chemistry untouched."""
    v2 = dict(v2)
    slug, tid = f"upload_{job_id}", "0"
    v2 = _normalize_v2(v2, slug, tid)                  # task_id/paper_id/anchor_*/extractor/is_multistep_synthesis
    v2.setdefault("has_reaction_template", bool(v2.get("reaction_templates")))
    v2.setdefault("molparser_detection_count", len(dets or []))
    v2.setdefault("synthesis_overview", None)
    v2.setdefault("texts", [])
    groups = (v2.get("selected_examples") or []) + (v2.get("reaction_templates") or [])
    # identifier_map: bbox_index -> identifier, harvested from every detection-derived molecule
    idmap = dict(v2.get("identifier_map") or {})
    for g in groups:
        for side in ("reactants", "products"):
            for mol in (g.get(side) or []):
                src = mol.get("_source") or {}
                bi, ident = src.get("bbox_index"), mol.get("identifier")
                if bi is not None and ident:
                    idmap.setdefault(str(bi), ident)
    v2["identifier_map"] = idmap
    # normalize metrics + reorder molecule/group keys
    for g in groups:
        if isinstance(g.get("metrics"), list):
            g["metrics"] = [gm for m in g["metrics"] for gm in _gt_metrics(m)]
        for side in ("reactants", "products"):
            if isinstance(g.get(side), list):
                g[side] = [_reorder(m, _MOL_ORDER) for m in g[side]]
    for key in ("selected_examples", "reaction_templates"):
        if isinstance(v2.get(key), list):
            v2[key] = [_reorder(g, _GRP_ORDER) for g in v2[key]]
    # drop our internal-only top fields that aren't part of the GT schema
    for k in ("reaction_class", "is_multistep", "_extractor"):
        v2.pop(k, None)
    return _reorder(v2, _GT_TOP_ORDER)


def _reactions_csv(v2: dict) -> str:
    """Flatten the V2 reactions into a chemist-friendly CSV (one row per reaction group): the natural
    deliverable when a chemist wants the result in a spreadsheet rather than raw JSON. Reactant/product
    columns hold dot-joined SMILES; conditions/metrics are flattened to their human text."""
    import csv as _csv
    def safe(value):
        text = "" if value is None else str(value)
        return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text
    def smis(mols):
        return " . ".join((m.get("E-SMILES") or m.get("smiles") or "").split("<sep>")[0]
                          for m in (mols or []) if isinstance(m, dict) and (m.get("E-SMILES") or m.get("smiles")))
    def cond_text(g):
        return " ; ".join(c["text"] for step in (g.get("conditions") or []) for c in (step or [])
                          if isinstance(c, dict) and c.get("text"))
    def metric_text(g):
        out = []
        for m in (g.get("metrics") or []):
            if not isinstance(m, dict):
                continue
            txt = m.get("text") or m.get("content")
            if txt:
                role = m.get("role") or m.get("type")
                out.append((role + ": " if role else "") + str(txt))
        return " ; ".join(out)
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["kind", "label", "reactants", "products", "conditions", "metrics"])
    for t in (v2.get("reaction_templates") or []):
        w.writerow([safe(value) for value in [
            "template", t.get("template_id") or t.get("identifier") or "",
            smis(t.get("reactants")), smis(t.get("products")), cond_text(t), metric_text(t),
        ]])
    for e in (v2.get("selected_examples") or []):
        w.writerow([safe(value) for value in [
            "example", e.get("instance_id") or e.get("identifier") or "",
            smis(e.get("reactants")), smis(e.get("products")), cond_text(e), metric_text(e),
        ]])
    return buf.getvalue()


def _paper_reactions_csv(pred: dict) -> str:
    """Flatten a paper-level RxnHarbor prediction (anchors[]) into one CSV row per reaction."""
    import csv as _csv

    def safe(value):
        text = "" if value is None else str(value)
        return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text

    def smis(mols):
        return " . ".join((m.get("E-SMILES") or m.get("smiles") or "").split("<sep>")[0]
                          for m in (mols or []) if isinstance(m, dict) and (m.get("E-SMILES") or m.get("smiles")))

    def cond_text(g):
        return " ; ".join(c["text"] for step in (g.get("conditions") or []) for c in (step or [])
                          if isinstance(c, dict) and c.get("text"))

    def metric_text(g):
        out = []
        for m in (g.get("metrics") or []):
            if isinstance(m, dict) and (m.get("text") or m.get("content")):
                role = m.get("role") or m.get("type")
                out.append((role + ": " if role else "") + str(m.get("text") or m.get("content")))
        return " ; ".join(out)

    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["anchor", "page", "kind", "reaction", "reactants", "products", "conditions", "metrics"])
    for anchor in (pred.get("anchors") or []):
        label = anchor.get("anchor_figure") or ""
        page = anchor.get("anchor_page") or ""
        for kind, key in (("template", "reaction_templates"), ("example", "selected_examples")):
            for group in (anchor.get(key) or []):
                row_label = (
                    group.get("template_id") or group.get("instance_id")
                    or group.get("identifier") or ""
                )
                w.writerow([safe(value) for value in [
                    label, page, kind, row_label,
                    smis(group.get("reactants")), smis(group.get("products")),
                    cond_text(group), metric_text(group),
                ]])
    return buf.getvalue()


def _export_reaction_rows(pred: dict, document_mode: bool) -> list[dict]:
    """Return one renderer-independent reaction list for report/RXN export.

    This is a view over the immutable prediction. It neither normalizes nor
    writes back chemistry, and it never consults GT or adjudication data.
    """
    containers = pred.get("anchors") if document_mode else [pred]
    rows: list[dict] = []
    for anchor_index, anchor in enumerate(containers or []):
        if not isinstance(anchor, dict):
            continue
        for kind, key in (("template", "reaction_templates"), ("example", "selected_examples")):
            for group_index, group in enumerate(anchor.get(key) or []):
                if not isinstance(group, dict):
                    continue

                def side(name):
                    values = []
                    for molecule in group.get(name) or []:
                        if not isinstance(molecule, dict):
                            continue
                        smiles = molecule.get("E-SMILES") or molecule.get("smiles")
                        if isinstance(smiles, str) and smiles.strip():
                            values.append(smiles.split("<sep>", 1)[0].strip())
                    return values

                conditions = [
                    str(condition.get("text"))
                    for step in (group.get("conditions") or [])
                    for condition in (step or [])
                    if isinstance(condition, dict) and condition.get("text") not in (None, "")
                ]
                metrics = [
                    {
                        "role": metric.get("role") or metric.get("type"),
                        "text": str(metric.get("text") or metric.get("content")),
                    }
                    for metric in (group.get("metrics") or [])
                    if isinstance(metric, dict) and (metric.get("text") or metric.get("content"))
                ]
                label = (
                    group.get("instance_id") or group.get("template_id")
                    or group.get("identifier") or f"{kind}-{group_index + 1}"
                )
                rows.append({
                    "anchor_index": anchor_index if document_mode else None,
                    "anchor": anchor.get("anchor_figure") if document_mode else None,
                    "page": anchor.get("anchor_page") if document_mode else None,
                    "kind": kind,
                    "label": str(label),
                    "reactants": side("reactants"),
                    "products": side("products"),
                    "conditions": conditions,
                    "metrics": metrics,
                })
    return rows


_SOURCE_RIGHTS_FIELDS = (
    "license",
    "attribution",
    "source_doi",
    "source_url",
    "license_url",
    "change_notice",
)
_CC_BY_4_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
_CC_BY_4_CHANGE_NOTICE = (
    "ChemRxn generated structured extraction and viewer metadata; "
    "source PDFs are unchanged."
)


def _source_rights(meta: dict) -> dict | None:
    """Validate one rights object without inventing missing metadata."""
    if not isinstance(meta, dict):
        return None
    if set(meta) - set(_SOURCE_RIGHTS_FIELDS):
        return None
    license_id = meta.get("license")
    if not isinstance(license_id, str) or license_id not in {
        "not-cleared-local-only",
        "cc-by-4.0",
    }:
        return None
    if license_id == "not-cleared-local-only":
        if any(
            meta.get(key) not in (None, "")
            for key in _SOURCE_RIGHTS_FIELDS
            if key != "license"
        ):
            return None
        return {"license": license_id}
    if license_id == "cc-by-4.0":
        attribution = meta.get("attribution")
        source_doi = meta.get("source_doi")
        source_url = meta.get("source_url")
        license_url = meta.get("license_url")
        change_notice = meta.get("change_notice")
        if (
            not isinstance(attribution, str)
            or not attribution.strip()
            or len(attribution) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in attribution)
            or not (source_doi or source_url)
            or license_url != _CC_BY_4_LICENSE_URL
            or not isinstance(change_notice, str)
            or not change_notice.strip()
        ):
            return None
        if source_doi is not None and (
            not isinstance(source_doi, str)
            or not re.fullmatch(r"10\.\d{4,9}/[^\s]+", source_doi)
        ):
            return None
        if source_url is not None:
            if not isinstance(source_url, str) or any(
                character.isspace() for character in source_url
            ):
                return None
            try:
                parsed = urllib.parse.urlsplit(source_url)
            except ValueError:
                return None
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                return None
    rights = {
        key: meta.get(key)
        for key in _SOURCE_RIGHTS_FIELDS
        if meta.get(key) not in (None, "")
    }
    return rights or None


_SOURCE_RIGHTS_CACHE: dict[tuple[str, str], tuple[tuple, dict | None, str | None]] = {}


def _job_source_rights(
    job_id: str,
    meta: dict,
    *,
    source_bytes: bytes | None = None,
    verify_pdf_view: bool = False,
) -> dict | None:
    """Resolve one importer proof; flat meta/collection rights are cache only.

    The proof is bound to the exact prediction, PDF view, validation record,
    handoff manifest/inventory, and mounted source PDFs. File-stat caching
    avoids re-hashing a long SI on every history poll. This detects accidental
    or partial local tampering; without an external signature it cannot defend
    against the same OS user consistently rewriting the entire proof chain.
    """
    del verify_pdf_view  # the proof always verifies both sealed structured payloads
    if not isinstance(job_id, str) or not job_id or not all(
        character.isalnum() or character in "._-" for character in job_id
    ):
        return None
    job_dir = JOBS_DIR / job_id
    work = job_dir / "work"
    paths = {
        "proof": work / "source_rights.json",
        "result": job_dir / "result.json",
        "view": work / "pdf_view.json",
        "validation": work / "validate.json",
        "manifest": work / "handoff_manifest.json",
        "inventory": work / "handoff_SHA256SUMS.txt",
        "main": work / "main.pdf",
        "si": work / "si.pdf",
    }

    def fingerprint() -> tuple:
        values = []
        for name, path in paths.items():
            try:
                stat = path.lstat()
                values.append((
                    name,
                    stat.st_size,
                    stat.st_mtime_ns,
                    path.is_file(),
                    path.is_symlink(),
                ))
            except OSError:
                values.append((name, None, None, False, False))
        return tuple(values)

    key = (str(JOBS_DIR.absolute()), job_id)
    current_fingerprint = fingerprint()
    cached = _SOURCE_RIGHTS_CACHE.get(key)
    if cached and cached[0] == current_fingerprint:
        rights, prediction_sha = cached[1], cached[2]
        if (
            rights
            and source_bytes is not None
            and hashlib.sha256(source_bytes).hexdigest() != prediction_sha
        ):
            return None
        return dict(rights) if rights else None

    def regular_bytes(name: str) -> bytes | None:
        path = paths[name]
        if not path.is_file() or path.is_symlink():
            return None
        return path.read_bytes()

    def sha256(value: object) -> bool:
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))

    def resolve() -> tuple[dict | None, str | None]:
        proof_bytes = regular_bytes("proof")
        result_bytes = regular_bytes("result")
        view_bytes = regular_bytes("view")
        validation_bytes = regular_bytes("validation")
        manifest_bytes = regular_bytes("manifest")
        inventory_bytes = regular_bytes("inventory")
        if any(
            value is None
            for value in (
                proof_bytes,
                result_bytes,
                view_bytes,
                validation_bytes,
                manifest_bytes,
                inventory_bytes,
            )
        ):
            return None, None
        if any(
            b"GT_SENTINEL" in value
            for value in (proof_bytes, validation_bytes, manifest_bytes, inventory_bytes)
        ):
            return None, None
        proof = _read_json(paths["proof"])
        if not isinstance(proof, dict) or set(proof) != {
            "schema",
            "status",
            "job",
            "rights",
            "anti_leakage",
            "hashes",
        }:
            return None, None
        if (
            proof.get("schema") != "chemrxn.source-rights-proof.v1"
            or proof.get("status") != "VALID"
        ):
            return None, None
        identity = proof.get("job")
        if not isinstance(identity, dict) or set(identity) != {
            "job_id",
            "paper_id",
            "variant",
            "handoff_schema",
        }:
            return None, None
        if (
            identity.get("job_id") != job_id
            or identity.get("variant") not in {"pdf_only", "pdf_si"}
            or not isinstance(identity.get("paper_id"), str)
            or not identity["paper_id"]
            or not isinstance(identity.get("handoff_schema"), str)
        ):
            return None, None
        anti = proof.get("anti_leakage")
        if anti != {
            "manifest_declares_gt_free": True,
            "gt_sidecars_mounted": False,
        }:
            return None, None
        hashes = proof.get("hashes")
        if not isinstance(hashes, dict) or set(hashes) != {
            "result_json",
            "pdf_view_json",
            "validation_json",
            "handoff_manifest",
            "handoff_inventory",
            "source_pdfs",
        }:
            return None, None
        source_hashes = hashes.get("source_pdfs")
        expected_sources = {"main"} if identity["variant"] == "pdf_only" else {"main", "si"}
        if (
            not isinstance(source_hashes, dict)
            or set(source_hashes) != expected_sources
            or not all(sha256(value) for value in source_hashes.values())
            or not all(
                sha256(hashes.get(name))
                for name in (
                    "result_json",
                    "pdf_view_json",
                    "validation_json",
                    "handoff_manifest",
                    "handoff_inventory",
                )
            )
        ):
            return None, None
        actual_hashes = {
            "result_json": hashlib.sha256(result_bytes).hexdigest(),
            "pdf_view_json": hashlib.sha256(view_bytes).hexdigest(),
            "validation_json": hashlib.sha256(validation_bytes).hexdigest(),
            "handoff_manifest": hashlib.sha256(manifest_bytes).hexdigest(),
            "handoff_inventory": hashlib.sha256(inventory_bytes).hexdigest(),
        }
        if any(actual_hashes[name] != hashes[name] for name in actual_hashes):
            return None, None
        for source in expected_sources:
            source_bytes_value = regular_bytes(source)
            if (
                source_bytes_value is None
                or hashlib.sha256(source_bytes_value).hexdigest() != source_hashes[source]
            ):
                return None, None
        try:
            inventory_text = inventory_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return None, None
        inventory: dict[str, str] = {}
        for line in inventory_text.splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})[ \t]+(?:\\*)?(.+)", line)
            if not match:
                return None, None
            digest, relative = match.groups()
            relative_path = Path(relative)
            if (
                relative in inventory
                or relative_path.is_absolute()
                or ".." in relative_path.parts
            ):
                return None, None
            inventory[relative] = digest
        if inventory.get("manifest.json") != hashes["handoff_manifest"]:
            return None, None
        variant_prefix = identity["variant"]
        if (
            inventory.get(f"{variant_prefix}/result.json") != hashes["result_json"]
            or inventory.get(f"{variant_prefix}/pdf_view.json")
            != hashes["pdf_view_json"]
        ):
            return None, None
        if any(
            inventory.get(f"source/{source}.pdf") != source_hashes[source]
            for source in expected_sources
        ):
            return None, None
        manifest = _read_json(paths["manifest"])
        validation = _read_json(paths["validation"])
        if (
            manifest.get("schema") != identity["handoff_schema"]
            or str(manifest.get("paper_id") or "") != identity["paper_id"]
            or manifest.get("anti_leakage") != {
                "gt": False,
                "reviewed": False,
                "score": False,
            }
            or validation.get("schema") != "chemrxn.local-handoff-validation.v1"
            or validation.get("status") != "VALID"
            or validation.get("variant") != identity["variant"]
            or validation.get("manifest_declares_gt_free") is not True
            or validation.get("gt_sidecars_mounted") is not False
            or validation.get("hashes") != {
                "result.json": hashes["result_json"],
                "pdf_view.json": hashes["pdf_view_json"],
            }
        ):
            return None, None
        proof_claim = proof.get("rights")
        if not isinstance(proof_claim, dict) or set(proof_claim) != set(_SOURCE_RIGHTS_FIELDS):
            return None, None
        proof_rights = _source_rights(proof_claim)
        validation_rights = _source_rights({
            key: validation.get(key)
            for key in _SOURCE_RIGHTS_FIELDS
        })
        manifest_claim = {
            key: manifest.get(key)
            for key in _SOURCE_RIGHTS_FIELDS
            if manifest.get(key) not in (None, "")
        }
        if manifest_claim.get("license") == "cc-by-4.0":
            manifest_claim.setdefault("license_url", _CC_BY_4_LICENSE_URL)
            manifest_claim.setdefault("change_notice", _CC_BY_4_CHANGE_NOTICE)
        manifest_rights = _source_rights(manifest_claim)
        if not proof_rights or proof_rights != validation_rights or proof_rights != manifest_rights:
            return None, None
        return proof_rights, hashes["result_json"]

    rights, prediction_sha = resolve()
    _SOURCE_RIGHTS_CACHE[key] = (current_fingerprint, rights, prediction_sha)
    if (
        rights
        and source_bytes is not None
        and hashlib.sha256(source_bytes).hexdigest() != prediction_sha
    ):
        return None
    return dict(rights) if rights else None


def _job_source_rights_status(job_id: str, meta: dict) -> str:
    if _job_source_rights(job_id, meta):
        return "verified"
    proof = JOBS_DIR / job_id / "work" / "source_rights.json"
    return "invalid" if proof.exists() or meta.get("source_mode") == "local_handoff" else "unavailable"


def _export_source_rights(
    job_id: str,
    meta: dict,
    source_bytes: bytes,
) -> dict | None:
    rights = _job_source_rights(job_id, meta, source_bytes=source_bytes)
    if rights:
        return rights
    if _job_source_rights_status(job_id, meta) == "invalid":
        raise HTTPException(
            409,
            "source rights proof is invalid; attribution-bearing export rejected",
        )
    return None


def _extraction_report(data: dict, job_id: str, meta: dict, source_bytes: bytes) -> dict:
    document_mode = meta.get("mode") == "pdf"
    reactions = _export_reaction_rows(data, document_mode)
    review = _read_json(JOBS_DIR / job_id / "review" / "adjudications.json")
    verdicts: dict[str, int] = {}
    if isinstance(review, dict):
        for item in review.get("items") or []:
            if isinstance(item, dict):
                verdict = str(item.get("verdict") or "unknown")
                verdicts[verdict] = verdicts.get(verdict, 0) + 1
    source = {
        "kind": "paper" if document_mode else "figure",
        "filename": meta.get("filename"),
        "prediction_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "immutable": True,
        "gt_included": False,
    }
    rights = _export_source_rights(job_id, meta, source_bytes)
    if rights:
        source["rights"] = rights
    return {
        "schema": "chemrxn.extraction-report.v1",
        "job_id": job_id,
        "source": source,
        "counts": {
            "reactions": len(reactions),
            "examples": sum(row["kind"] == "example" for row in reactions),
            "templates": sum(row["kind"] == "template" for row in reactions),
            "human_adjudications": sum(verdicts.values()),
        },
        "human_review": {"verdict_counts": verdicts},
        "reactions": reactions,
    }


def _rxn_export(data: dict, job_id: str, meta: dict, source_bytes: bytes) -> Response:
    """Generate a ZIP of valid MDL RXN records plus an omission manifest.

    Markush/unresolved reactions that RDKit cannot parse are reported rather
    than silently coerced into a different structure.
    """
    try:
        from rdkit.Chem import rdChemReactions
    except Exception:
        raise HTTPException(503, "RDKit reaction export is unavailable; run `chemrxn setup`") from None

    rows = _export_reaction_rows(data, meta.get("mode") == "pdf")
    manifest = {
        "schema": "chemrxn.rxn-export.v1",
        "job_id": job_id,
        "source_prediction_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "gt_included": False,
        "exported": [],
        "omitted": [],
    }
    rights = _export_source_rights(job_id, meta, source_bytes)
    if rights:
        manifest["source_rights"] = rights
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, row in enumerate(rows, 1):
            reaction_smiles = f"{'.'.join(row['reactants'])}>>{'.'.join(row['products'])}"
            reason = None
            block = None
            if not row["reactants"] or not row["products"]:
                reason = "missing reactant or product SMILES"
            else:
                try:
                    reaction = rdChemReactions.ReactionFromSmarts(reaction_smiles, useSmiles=True)
                    if reaction is None:
                        reason = "RDKit could not parse the reaction SMILES"
                    else:
                        block = rdChemReactions.ReactionToRxnBlock(reaction)
                except Exception as exc:
                    reason = f"RDKit export failed: {type(exc).__name__}"
            record = {
                "index": index,
                "anchor": row["anchor"],
                "page": row["page"],
                "kind": row["kind"],
                "label": row["label"],
                "reaction_smiles": reaction_smiles,
            }
            if block:
                safe_label = "".join(
                    character if character.isalnum() or character in "._-" else "_"
                    for character in row["label"]
                ).strip("._") or f"reaction_{index}"
                filename = f"{index:04d}_{safe_label[:80]}.rxn"
                archive.writestr(filename, block)
                manifest["exported"].append({**record, "file": filename})
            else:
                manifest["omitted"].append({**record, "reason": reason or "unknown export failure"})
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return Response(
        output.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{job_id}_rxn.zip"'},
    )


@app.get("/api/jobs/{job_id}/export")
def api_export(job_id: str, fmt: str = "json", request: Request = None):
    """Download the extraction. `fmt=json` (default): GT-schema V2 (02_v2_schema.md) — pretty-printed
    (indent=2), GT key order, GT-shape metrics, identifier_map; a drop-in `*_predicted_output.json`
    re-uploadable into the chem_reviewer / Ketcher pipeline. `fmt=csv`: a flat reactions table for
    spreadsheets (one row per reaction group). A DOCUMENT job (mode="pdf") exports its original
    paper-level RxnHarbor prediction unchanged — never a guessed figure schema."""
    meta = _read_json(JOBS_DIR / job_id / "meta.json") or JOBS.get(job_id) or {}
    _require_job_owner(meta, request=request)
    source_path = JOBS_DIR / job_id / "result.json"
    data = _read_json(source_path)
    if data is None:
        raise HTTPException(404, "no result yet")
    if fmt not in ("json", "v2", "csv", "report", "rxn", "rights"):
        raise HTTPException(400, "format must be one of v2, csv, report, rxn, or rights")
    source_bytes = source_path.read_bytes()
    if b"GT_SENTINEL" in source_bytes:
        raise HTTPException(409, "prediction export rejected: GT_SENTINEL was found in result.json")
    _assert_gt_free(data)
    if fmt == "rights":
        rights = _export_source_rights(job_id, meta, source_bytes)
        if not rights:
            raise HTTPException(409, "source rights metadata is unavailable for this job")
        return JSONResponse(
            {
                "schema": "chemrxn.source-rights.v1",
                "job_id": job_id,
                "source_prediction_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "source_rights": rights,
                "raw_prediction_immutable": True,
                "gt_included": False,
            },
            headers={"Content-Disposition": f'attachment; filename="{job_id}_rights.json"'},
        )
    if fmt == "report":
        return JSONResponse(
            _extraction_report(data, job_id, meta, source_bytes),
            headers={"Content-Disposition": f'attachment; filename="{job_id}_report.json"'},
        )
    if fmt == "rxn":
        return _rxn_export(data, job_id, meta, source_bytes)
    if meta.get("mode") == "pdf":
        if fmt == "csv":
            return Response(_paper_reactions_csv(data), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{job_id}_reactions.csv"'})
        return Response(source_bytes, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{job_id}_paper_pred.json"'})
    if fmt == "csv":
        return Response(_reactions_csv(data), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{job_id}_reactions.csv"'})
    dets = _read_json(JOBS_DIR / job_id / "work" / "0_molparser.json") or []
    body = json.dumps(_to_gt_v2(data, job_id, dets), indent=2, ensure_ascii=False)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{job_id}_v2.json"'})


# ─────────────────────────────── document (PDF) mode ───────────────────────────────
# One job per PAPER (registered by harness/scripts/preds_to_viewer.py): result.json = the scored
# paper-level RxnHarbor prediction (unchanged), work/main.pdf = the paper, work/pdf_view.json = a
# viewer-only geometry index. These endpoints are additive — every figure-job endpoint above is
# untouched, and every endpoint here 404s for a non-PDF job.

def _require_job_owner(meta: dict, request: Request = None, owner: str = None) -> None:
    """Enforce the persisted local-user boundary without weakening internal helpers."""
    recorded_owner = meta.get("owner")
    if owner is not None:
        allowed_owners = {owner}
    elif request is not None:
        allowed_owners = {
            value for value in (_identity(request), LOCAL_RUNTIME_USER_ID)
            if value
        }
    else:
        return
    if recorded_owner and recorded_owner not in allowed_owners:
        raise HTTPException(403, "not your job")


def _pdf_job(job_id: str, request: Request = None, owner: str = None):
    """(job_dir, meta) for a document-mode job. 404 for unknown ids, malformed ids, and figure jobs."""
    if not job_id or not all(c.isalnum() or c in "._-" for c in job_id) or job_id.startswith("."):
        raise HTTPException(404, "unknown job")
    jdir = JOBS_DIR / job_id
    meta = _read_json(jdir / "meta.json") or {}
    if meta.get("mode") != "pdf":
        raise HTTPException(404, "not a PDF document job")
    _require_job_owner(meta, request=request, owner=owner)
    return jdir, meta


_PDF_META_PUBLIC = ("id", "filename", "mode", "status", "note", "eval_r", "pdf_score",
                    "n_pages", "n_anchors", "n_reactions", "created", "provider", "model_name", "tag")


def _public_pdf_validation(value):
    """Remove rights claims from the validator's public compatibility payload.

    Source rights have one authoritative API projection: ``job.source_rights``
    after proof verification. Validation remains useful for counts, warnings,
    and anchor diagnostics, but must never become a second, unverified rights
    channel.
    """
    if isinstance(value, dict):
        return {
            key: _public_pdf_validation(item)
            for key, item in value.items()
            if key not in _SOURCE_RIGHTS_FIELDS and key != "source_rights"
        }
    if isinstance(value, list):
        return [_public_pdf_validation(item) for item in value]
    return value


@app.get("/api/jobs/{job_id}/pdf/meta")
def api_pdf_meta(job_id: str, request: Request = None):
    """The document view index: job card fields + pdf_view.json (pages / layout regions with
    not-extracted marking / anchors / molecule boxes / text claims). Chemistry itself is served by
    /result and /pdf/anchor — this is geometry only. Adds: gt_attached (a post-hoc gt/compare.json
    sidecar exists), per-anchor validator warnings, and the reviewer base URL (never hardcode the
    reviewer port client-side)."""
    jdir, meta = _pdf_job(job_id, request=request)
    view = _load_reconciled_pdf_view(jdir)
    if view is None:
        raise HTTPException(404, "no pdf_view.json for this job")
    for a in (view.get("anchors") or []):
        slug = a.get("reviewer_slug")
        # verify the reviewer task actually exists under the configured store before showing a
        # link; when the store isn't visible from this process, trust the registration record
        a["has_reviewer"] = bool(slug) and ((PDF_ROOT / slug).is_dir() if PDF_ROOT.exists() else True)
    validate = _read_json(jdir / "work" / "validate.json")
    validation_counts = validate.get("counts") if isinstance(validate, dict) else None
    stats = view.get("stats") if isinstance(view.get("stats"), dict) else None
    if isinstance(validation_counts, dict) and isinstance(stats, dict):
        located = validation_counts.get("molecule_occurrences_located")
        unlocated = validation_counts.get("molecule_occurrences_derived_unlocated")
        if isinstance(located, int) and located >= 0:
            stats.setdefault("n_molecules_located", located)
        if isinstance(unlocated, int) and unlocated >= 0:
            # This is an API projection only. The sealed pdf_view.json bytes
            # remain untouched, including for older handoffs without the key.
            stats.setdefault("n_molecules_unlocated", unlocated)
            stats["n_molecules_derived_unlocated"] = unlocated
    document_catalog = _pdf_document_catalog(jdir, view)
    public_job = {k: meta.get(k) for k in _PDF_META_PUBLIC}
    public_job["source_rights"] = _job_source_rights(
        job_id,
        meta,
        source_bytes=(jdir / "result.json").read_bytes(),
        verify_pdf_view=True,
    )
    public_job["source_rights_status"] = _job_source_rights_status(job_id, meta)
    return {"job": public_job,
            "gt_attached": (jdir / "gt" / "compare.json").exists(),
            "reviewer_v2_available": (jdir / "work" / "reviewer_v2.json").exists(),
            "reviewer_base": REVIEWER_BASE,
            "validate": _public_pdf_validation(validate),
            "documents": document_catalog,
            "view": view}


@app.get("/api/jobs/{job_id}/pdf/compare")
def api_pdf_compare(job_id: str, request: Request = None):
    """The post-hoc prediction-vs-GT comparison sidecar (written by import_pdf_result.py --with-gt
    using the frozen scorer). 404 when no GT was explicitly attached — the default import never
    reads GT, so this endpoint existing for a job is itself the post-hoc marker."""
    jdir, _ = _pdf_job(job_id, request=request)
    cmp_p = jdir / "gt" / "compare.json"
    data = _read_json(cmp_p)
    if data is None:
        raise HTTPException(404, "no GT attached to this job (import with --with-gt --gt-root)")
    return data


_ADJUDICATION_VERDICTS = {
    "extraction error",
    "OCSR error",
    "R-group substitution error",
    "GT issue",
    "granularity-association issue",
    "uncertain",
    "prediction accepted",
}

_PRODUCT_REVIEW_VERDICTS = {
    "accept as extracted",
    "exclude from review set",
    "mark uncertain",
    "correct fields",
    "split reaction record",
    "merge with adjacent record",
}

# A version is one explicit human Apply.  Five hundred and twelve edits is
# comfortably above the largest current per-paper review while bounding both
# directory scans and fail-closed lineage validation for a local service.
_PDF_REVIEW_MAX_VERSIONS = 512


@dataclass
class _PdfReviewValidationContext:
    """Request-local memoization for immutable review-chain validation.

    This cache never crosses requests or mutations.  It removes repeated PDF
    reads while walking cumulative snapshots, without weakening the final
    fresh-hash check performed by Apply/Undo before publishing a new head.
    """

    jdir: Path
    source_hashes: Optional[dict] = None
    rows: Optional[set[str]] = None
    raw_result: Optional[dict] = None
    pdf_view: Optional[dict] = None
    reviewer_evidence: Optional[dict] = None
    document_catalog: Optional[list[dict]] = None
    pdf_pages: dict[str, list[dict]] = field(default_factory=dict)
    page_dimensions: dict[tuple[str, int], tuple[float, float]] = field(
        default_factory=dict,
    )
    raw_snapshots: dict[int, Optional[dict]] = field(default_factory=dict)
    validated_snapshots: dict[int, dict] = field(default_factory=dict)
    validating_versions: set[int] = field(default_factory=set)
    materialized_version: Optional[int] = None
    materialized_state: Optional[dict] = None


def _empty_adjudications() -> dict:
    return {"schema": "pdf_adjudications_v1", "items": []}


@app.get("/api/jobs/{job_id}/pdf/evidence")
def api_pdf_evidence(job_id: str, request: Request = None):
    """GT-free, perception-derived evidence model for the shared Vue reviewer island."""
    jdir, _ = _pdf_job(job_id, request=request)
    target = jdir / "work" / "reviewer_v2.json"
    data = _read_json(target)
    if data is None:
        # Older live runtime jobs predate the derived reviewer sidecar. Rebuild
        # only that GT-free projection from their immutable prediction and
        # geometry; never rewrite result.json or pdf_view.json.
        prediction = _read_json(jdir / "result.json")
        pdf_view = _read_json(jdir / "work" / "pdf_view.json")
        if isinstance(prediction, dict) and isinstance(pdf_view, dict):
            scripts_dir = REPO / "scripts"
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            import preds_to_viewer as pdf_view_adapter
            perception_roots = {
                source: root
                for source in ("main", "si")
                if (root := jdir / "work" / "perception" / source).exists()
            }
            data = _runtime_pdf_reviewer_data(
                pdf_view_adapter,
                prediction,
                _reconcile_pdf_view_anchor_index(pdf_view, prediction),
                perception_roots,
            )
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, target)
    if data is None:
        raise HTTPException(404, "reviewer_v2 evidence is unavailable; re-import this job")
    # Existing jobs may predate chemistry-entity identities.  Enrich only the
    # GET response: reviewer_v2.json, result.json and pdf_view.json remain
    # byte-identical on disk.
    scripts_dir = REPO / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import preds_to_viewer as pdf_view_adapter
    return pdf_view_adapter._attach_reviewer_entity_identities(data)  # noqa: SLF001


@app.get("/api/jobs/{job_id}/pdf/adjudications")
def api_pdf_adjudications(job_id: str, request: Request = None):
    jdir, _ = _pdf_job(job_id, request=request)
    return _read_json(jdir / "review" / "adjudications.json") or _empty_adjudications()


@app.put("/api/jobs/{job_id}/pdf/adjudications")
async def api_pdf_save_adjudications(job_id: str, request: Request):
    """Atomically save isolated human verdicts without touching prediction, GT or perception."""
    jdir, _ = _pdf_job(job_id, request=request)
    body = await request.json()
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list) or len(items) > 2000:
        raise HTTPException(400, "items must be a list with at most 2000 entries")
    cleaned = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(400, "each adjudication must be an object")
        difference_id = str(item.get("difference_id") or "").strip()
        verdict = str(item.get("verdict") or "").strip()
        if not difference_id or len(difference_id) > 200 or difference_id in seen:
            raise HTTPException(400, "difference_id must be unique and at most 200 characters")
        if verdict not in _ADJUDICATION_VERDICTS:
            raise HTTPException(400, f"unsupported verdict: {verdict}")
        seen.add(difference_id)
        note = str(item.get("note") or "")[:4000]
        correction_in = item.get("correction") if isinstance(item.get("correction"), dict) else {}
        correction = {
            key: str(correction_in[key])[:12000]
            for key in ("smiles", "condition_text")
            if correction_in.get(key) is not None
        }
        cleaned.append({
            "difference_id": difference_id,
            "verdict": verdict,
            "note": note,
            "correction": correction or None,
            "updated_at": float(item.get("updated_at") or time.time()),
        })
    payload = {"schema": "pdf_adjudications_v1", "items": cleaned}
    with _pdf_review_mutation_lock(jdir):
        review_dir = jdir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        if (review_dir / "baseline-v0.json").exists() or _pdf_review_head(jdir):
            raise HTTPException(
                409,
                "versioned PDF review has started; save through Review changes and Apply",
            )
        target = review_dir / "adjudications.json"
        tmp = review_dir / f".adjudications.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, target)
    return payload


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pdf_review_source_hashes(
    jdir: Path,
    context: Optional[_PdfReviewValidationContext] = None,
) -> dict:
    if context is not None and context.source_hashes is not None:
        return context.source_hashes
    result = jdir / "result.json"
    pdf_view = jdir / "work" / "pdf_view.json"
    reviewer_v2 = jdir / "work" / "reviewer_v2.json"
    if not result.is_file() or not pdf_view.is_file() or not reviewer_v2.is_file():
        raise HTTPException(409, "immutable PDF review sources are unavailable")
    if context is not None and context.pdf_view is not None:
        view = context.pdf_view
    else:
        view = _read_json(pdf_view) or {}
        if context is not None and isinstance(view, dict):
            context.pdf_view = view
    stored_documents = _read_json(jdir / "work" / "documents.json") or {}
    records = (
        stored_documents.get("documents")
        if isinstance(stored_documents.get("documents"), list)
        else []
    )
    expects_si = any(
        isinstance(item, dict) and item.get("source") == "si"
        for item in records
    ) or any(
        isinstance(item, dict)
        and (item.get("source_document") or item.get("source")) == "si"
        for item in (view.get("pages") or [])
    )
    hashes = {
        "result_sha256": _sha256_file(result),
        "pdf_view_sha256": _sha256_file(pdf_view),
        "reviewer_v2_sha256": _sha256_file(reviewer_v2),
    }
    for source in ("main", "si"):
        pdf = jdir / "work" / f"{source}.pdf"
        required = source == "main" or expects_si
        if required and not pdf.is_file():
            raise HTTPException(
                409,
                f"immutable {source} PDF review source is unavailable",
            )
        if not pdf.is_file():
            continue
        hashes[f"{source}_pdf_sha256"] = _sha256_file(pdf)
    if context is not None:
        context.source_hashes = hashes
    return hashes


def _pdf_review_snapshot_files(jdir: Path) -> list[tuple[int, Path]]:
    review_dir = jdir / "review"
    if not review_dir.is_dir():
        return []
    output = []
    for target in review_dir.glob("reviewed-v*.json"):
        match = re.fullmatch(r"reviewed-v([1-9][0-9]*)\.json", target.name)
        if match:
            version = int(match.group(1))
            if version > _PDF_REVIEW_MAX_VERSIONS:
                raise HTTPException(
                    409,
                    f"PDF review history exceeds the {_PDF_REVIEW_MAX_VERSIONS}-version limit",
                )
            output.append((version, target))
            if len(output) > _PDF_REVIEW_MAX_VERSIONS:
                raise HTTPException(
                    409,
                    f"PDF review history exceeds the {_PDF_REVIEW_MAX_VERSIONS}-version limit",
                )
    return sorted(output)


def _pdf_review_head(jdir: Path) -> int:
    path = jdir / "review" / "current.json"
    if not path.exists():
        return 0
    current = _read_json(path)
    value = current.get("head_version") if isinstance(current, dict) else None
    if (
        not isinstance(current, dict)
        or current.get("schema") != "chemrxn.pdf-review-head.v1"
        or type(value) is not int
        or value < 0
        or value > _PDF_REVIEW_MAX_VERSIONS
    ):
        raise HTTPException(409, "PDF review head is invalid")
    return value


def _pdf_review_snapshot(
    jdir: Path,
    version: int,
    context: Optional[_PdfReviewValidationContext] = None,
) -> Optional[dict]:
    if context is not None and version in context.raw_snapshots:
        return context.raw_snapshots[version]
    target = (
        jdir / "review" / "baseline-v0.json"
        if version == 0
        else jdir / "review" / f"reviewed-v{version}.json"
    )
    snapshot = _read_json(target)
    if context is not None:
        context.raw_snapshots[version] = snapshot
    return snapshot


def _require_pdf_review_snapshot_source(
    jdir: Path,
    snapshot: dict,
    context: Optional[_PdfReviewValidationContext] = None,
) -> dict:
    current = _pdf_review_source_hashes(jdir, context)
    version = snapshot.get("version") if isinstance(snapshot, dict) else None
    if isinstance(version, int) and version >= 0:
        expected = snapshot.get("source")
        if not isinstance(expected, dict) or expected != current:
            raise HTTPException(
                409,
                "active review snapshot does not match immutable PDF sources",
            )
    return current


def _clean_pdf_molecule_correction(value) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(400, "molecule correction must be an object")
    text_fields = (
        "document_id", "source_document", "page_id", "anchor_id",
        "reaction_id", "role", "occurrence_id", "esmiles",
    )
    cleaned = {key: str(value.get(key) or "").strip() for key in text_fields}
    if cleaned["source_document"] not in ("main", "si"):
        raise HTTPException(400, "molecule source_document must be main or si")
    if cleaned["role"] not in ("reactant", "product", "condition"):
        raise HTTPException(400, "unsupported molecule occurrence role")
    if not cleaned["esmiles"] or len(cleaned["esmiles"]) > 12000:
        raise HTTPException(
            400,
            "reviewed E-SMILES is required and must be at most 12000 characters",
        )
    if "<sep>" not in cleaned["esmiles"]:
        raise HTTPException(
            400,
            "reviewed Ketcher output must retain the <sep> separator",
        )
    for key in ("page", "anchor", "reaction", "molecule_index"):
        number = value.get(key)
        minimum = 1 if key == "page" else 0
        if not isinstance(number, int) or number < minimum:
            qualifier = "positive" if key == "page" else "non-negative"
            raise HTTPException(400, f"molecule {key} must be a {qualifier} integer")
        cleaned[key] = number
    path = value.get("molecule_path")
    if (not isinstance(path, list) or len(path) != 7
            or not all(isinstance(part, (str, int)) for part in path)):
        raise HTTPException(400, "molecule_path must identify one V2 E-SMILES field")
    cleaned["molecule_path"] = path
    for key in text_fields[:-1]:
        if not cleaned[key] or len(cleaned[key]) > 240:
            raise HTTPException(400, f"molecule {key} is required and too long")
    return cleaned


def _clean_pdf_geometry_correction(value) -> dict:
    """Clean one proposed PDF location without trusting its target identity.

    The immutable reviewer row is resolved again in
    ``_validate_pdf_geometry_correction``.  In particular, the client cannot
    supply the original location that will be recorded in the sidecar.
    """
    if not isinstance(value, dict):
        raise HTTPException(400, "geometry correction must be an object")
    target_type = str(value.get("target_type") or "").strip()
    if target_type not in ("molecule", "condition", "metric"):
        raise HTTPException(400, "geometry target_type must be molecule, condition, or metric")
    target_id = str(value.get("target_id") or "").strip()
    if not target_id or len(target_id) > 320:
        raise HTTPException(400, "geometry target_id is required and too long")
    target_path = value.get("target_path")
    if (
        not isinstance(target_path, list)
        or not 2 <= len(target_path) <= 3
        or not all(
            isinstance(part, str) or (type(part) is int and part >= 0)
            for part in target_path
        )
    ):
        raise HTTPException(400, "geometry target_path is invalid")
    source_document = str(value.get("source_document") or "").strip()
    if source_document not in ("main", "si"):
        raise HTTPException(400, "geometry source_document must be main or si")
    page = value.get("page")
    if type(page) is not int or page < 1:
        raise HTTPException(400, "geometry page must be a positive integer")
    bbox_frame = str(value.get("bbox_frame") or "").strip()
    if bbox_frame != "pdf_points":
        raise HTTPException(400, "geometry bbox_frame must be pdf_points")
    bbox = value.get("bbox")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(
            isinstance(number, (int, float))
            and not isinstance(number, bool)
            and math.isfinite(float(number))
            for number in bbox
        )
    ):
        raise HTTPException(400, "geometry bbox must contain four finite numbers")
    cleaned_bbox = [float(number) for number in bbox]
    if cleaned_bbox[0] >= cleaned_bbox[2] or cleaned_bbox[1] >= cleaned_bbox[3]:
        raise HTTPException(400, "geometry bbox must have positive width and height")
    return {
        "schema": "chemrxn.pdf-geometry-correction.v1",
        "target_type": target_type,
        "target_id": target_id,
        "target_path": target_path,
        "source_document": source_document,
        "page": page,
        "bbox_frame": bbox_frame,
        "bbox": cleaned_bbox,
    }


def _clean_product_review_item(item) -> dict:
    if not isinstance(item, dict):
        raise HTTPException(400, "review correction must be an object")
    difference_id = str(item.get("difference_id") or "").strip()
    verdict = str(item.get("verdict") or "").strip()
    if not difference_id or len(difference_id) > 200:
        raise HTTPException(400, "difference_id is required and must be at most 200 characters")
    if verdict not in _PRODUCT_REVIEW_VERDICTS:
        raise HTTPException(400, f"unsupported product verdict: {verdict}")
    correction_in = item.get("correction") if isinstance(item.get("correction"), dict) else {}
    correction = {
        key: str(correction_in[key])[:12000]
        for key in ("smiles", "condition_text")
        if correction_in.get(key) is not None
    }
    if correction_in.get("molecule") is not None:
        correction["molecule"] = _clean_pdf_molecule_correction(
            correction_in["molecule"],
        )
    return {
        "difference_id": difference_id,
        "verdict": verdict,
        "note": str(item.get("note") or "")[:4000],
        "correction": correction or None,
        "updated_at": float(item.get("updated_at") or time.time()),
    }


def _clean_review_chain_adjudication(item) -> tuple[dict, bool]:
    """Canonicalize current product edits and historical reviewer verdicts.

    The pre-versioned ``adjudications.json`` endpoint used a different verdict
    vocabulary.  Historical verdict text is inert data, so preserve bounded
    unknown labels as legacy records instead of silently dropping human work.
    The returned boolean identifies product-vocabulary entries.
    """
    verdict = str(item.get("verdict") or "").strip() if isinstance(item, dict) else ""
    if verdict in _PRODUCT_REVIEW_VERDICTS:
        return _clean_product_review_item(item), True
    if not isinstance(item, dict):
        raise HTTPException(400, "legacy adjudication must be an object")
    difference_id = str(item.get("difference_id") or "").strip()
    if not difference_id or len(difference_id) > 200:
        raise HTTPException(400, "legacy difference_id is required and too long")
    if verdict not in _ADJUDICATION_VERDICTS:
        raise HTTPException(400, "legacy adjudication verdict is not recognized")
    correction_in = item.get("correction")
    if correction_in is not None and not isinstance(correction_in, dict):
        raise HTTPException(400, "legacy adjudication correction is invalid")
    correction = {
        key: str(correction_in[key])[:12000]
        for key in ("smiles", "condition_text")
        if isinstance(correction_in, dict) and correction_in.get(key) is not None
    }
    updated_at = item.get("updated_at")
    if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool):
        raise HTTPException(400, "legacy adjudication updated_at is invalid")
    return {
        "difference_id": difference_id,
        "verdict": verdict,
        "note": str(item.get("note") or "")[:4000],
        "correction": correction or None,
        "updated_at": float(updated_at),
    }, False


def _pdf_review_baseline_digest(snapshot: dict) -> str:
    payload = {
        key: snapshot.get(key)
        for key in (
            "schema", "version", "parent_version", "source",
            "adjudications", "geometry_corrections", "migration",
        )
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _pdf_review_legacy_items(jdir: Path) -> list[dict]:
    payload = _read_json(jdir / "review" / "adjudications.json")
    if payload is None:
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "pdf_adjudications_v1"
        or not isinstance(items, list)
        or len(items) > 2000
    ):
        raise HTTPException(409, "legacy PDF adjudications cannot be migrated safely")
    canonical = []
    seen = set()
    for item in items:
        try:
            cleaned, _ = _clean_review_chain_adjudication(item)
        except (HTTPException, TypeError, ValueError) as exc:
            raise HTTPException(
                409,
                "legacy PDF adjudications cannot be migrated safely",
            ) from exc
        difference_id = cleaned["difference_id"]
        if difference_id in seen or cleaned != item:
            raise HTTPException(
                409,
                "legacy PDF adjudications cannot be migrated safely",
            )
        seen.add(difference_id)
        canonical.append(cleaned)
    return canonical


def _pdf_review_transient_baseline(
    jdir: Path,
    context: Optional[_PdfReviewValidationContext] = None,
) -> dict:
    snapshot = {
        "schema": "chemrxn.pdf-review-baseline.v1",
        "version": 0,
        "parent_version": None,
        "source": _pdf_review_source_hashes(jdir, context),
        "adjudications": _pdf_review_legacy_items(jdir),
        "geometry_corrections": [],
    }
    digest = _pdf_review_baseline_digest(snapshot)
    snapshot["baseline_id"] = digest
    snapshot["patch_id"] = f"sealed-prediction:{digest[:12]}"
    return snapshot


def _pdf_review_upgrade_baseline(
    jdir: Path,
    context: Optional[_PdfReviewValidationContext] = None,
) -> dict:
    """Derive the empty v0 contract used by pre-baseline reviewed-vN chains.

    Before baseline-v0 existed, the server always treated v0 as empty.  A
    genuine old v1 therefore has one proposal with ``before: null`` and one
    corresponding cumulative state entry.  Anything else fails closed rather
    than guessing at historical human state.
    """
    files = _pdf_review_snapshot_files(jdir)
    if not files or files[0][0] != 1:
        raise HTTPException(409, "PDF review v0 baseline cannot be upgraded safely")
    snapshot = _pdf_review_snapshot(jdir, 1, context)
    diff = _read_json(jdir / "review" / "diff-v1.json")
    current_source = _pdf_review_source_hashes(jdir, context)
    proposal = snapshot.get("proposal") if isinstance(snapshot, dict) else None
    proposal_id = proposal.get("proposal_id") if isinstance(proposal, dict) else None
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema") != "chemrxn.pdf-reviewed-snapshot.v1"
        or snapshot.get("version") != 1
        or snapshot.get("parent_version") != 0
        or snapshot.get("base_patch_id") != "sealed-prediction"
        or snapshot.get("raw_prediction_mutated") is not False
        or snapshot.get("source") != current_source
        or not isinstance(proposal, dict)
        or proposal.get("schema") != "chemrxn.proposed-review-patch.v1"
        or proposal.get("base_version") != 0
        or proposal.get("base_patch_id") != "sealed-prediction"
        or proposal.get("before") is not None
        or proposal.get("source") != current_source
        or proposal.get("raw_prediction_mutated") is not False
        or not isinstance(proposal_id, str)
        or not secrets.compare_digest(
            proposal_id,
            _pdf_review_proposal_digest(proposal),
        )
        or snapshot.get("patch_id") != f"reviewed-v1:{proposal_id[:12]}"
        or not isinstance(diff, dict)
        or diff.get("schema") != "chemrxn.pdf-review-diff.v1"
        or diff.get("version") != 1
        or diff.get("proposal") != proposal
    ):
        raise HTTPException(409, "PDF review v0 baseline cannot be upgraded safely")
    operation = proposal.get("operation")
    if operation == "review_correction":
        expected_adjudications = [proposal.get("after")]
        expected_geometry = []
    elif operation == "locate_pdf_geometry":
        after = proposal.get("after")
        expected_adjudications = []
        expected_geometry = (
            [{**after, "reviewed_version": 1}]
            if isinstance(after, dict)
            else None
        )
    else:
        expected_adjudications = expected_geometry = None
    if (
        snapshot.get("adjudications") != expected_adjudications
        or snapshot.get("geometry_corrections") != expected_geometry
    ):
        raise HTTPException(409, "PDF review v0 baseline cannot be upgraded safely")
    baseline = {
        "schema": "chemrxn.pdf-review-baseline.v1",
        "version": 0,
        "parent_version": None,
        "source": current_source,
        "adjudications": [],
        "geometry_corrections": [],
        "migration": "pre-baseline-version-chain",
    }
    baseline["baseline_id"] = _pdf_review_baseline_digest(baseline)
    # Preserve the exact parent patch id recorded by existing immutable v1.
    baseline["patch_id"] = "sealed-prediction"
    return baseline


def _ensure_pdf_review_baseline(
    jdir: Path,
    context: Optional[_PdfReviewValidationContext] = None,
) -> dict:
    target = jdir / "review" / "baseline-v0.json"
    if target.is_file():
        baseline = _pdf_review_snapshot(jdir, 0, context)
        return baseline if isinstance(baseline, dict) else {}
    baseline = (
        _pdf_review_upgrade_baseline(jdir, context)
        if _pdf_review_snapshot_files(jdir)
        else _pdf_review_transient_baseline(jdir, context)
    )
    _exclusive_json(target, baseline)
    if context is not None:
        context.raw_snapshots[0] = baseline
    return baseline


def _pdf_review_rows(
    jdir: Path,
    context: Optional[_PdfReviewValidationContext] = None,
) -> set[str]:
    if context is not None and context.rows is not None:
        return context.rows
    if context is not None and context.raw_result is not None:
        result = context.raw_result
    else:
        result = _read_json(jdir / "result.json")
        if context is not None and isinstance(result, dict):
            context.raw_result = result
    rows = {
        f"pred:{anchor_index}:{reaction_index}"
        for anchor_index, anchor in enumerate(
            (result.get("anchors") if isinstance(result, dict) else []) or []
        )
        if isinstance(anchor, dict)
        for reaction_index, reaction in enumerate(
            anchor.get("selected_examples") or []
        )
        if isinstance(reaction, dict)
    }
    if context is not None:
        context.rows = rows
    return rows


def _pdf_review_raw_reaction(
    jdir: Path,
    difference_id: str,
    context: Optional[_PdfReviewValidationContext] = None,
) -> Optional[tuple[dict, dict, int, int]]:
    match = re.fullmatch(r"pred:([0-9]+):([0-9]+)", difference_id)
    if context is not None and context.raw_result is not None:
        result = context.raw_result
    else:
        result = _read_json(jdir / "result.json")
        if context is not None and isinstance(result, dict):
            context.raw_result = result
    if not match or not isinstance(result, dict):
        return None
    anchor_index, reaction_index = map(int, match.groups())
    anchors = result.get("anchors") if isinstance(result.get("anchors"), list) else []
    if anchor_index >= len(anchors) or not isinstance(anchors[anchor_index], dict):
        return None
    anchor = anchors[anchor_index]
    reactions = (
        anchor.get("selected_examples")
        if isinstance(anchor.get("selected_examples"), list)
        else []
    )
    if (
        reaction_index >= len(reactions)
        or not isinstance(reactions[reaction_index], dict)
    ):
        return None
    return anchor, reactions[reaction_index], anchor_index, reaction_index


def _review_geometry_source(value: dict, row: dict) -> dict:
    source = {}
    if isinstance(value.get("source"), dict):
        source.update(value["source"])
    if isinstance(value.get("_source"), dict):
        source.update(value["_source"])
    occurrence = value.get("occurrence") if isinstance(value.get("occurrence"), dict) else {}
    source_document = (
        source.get("source_document")
        or source.get("src")
        or occurrence.get("source_document")
        or row.get("source_document")
        or "main"
    )
    if source_document not in ("main", "si"):
        source_document = "main"
    page = (
        value.get("bbox_page")
        or source.get("bbox_page")
        or source.get("page")
        or occurrence.get("page")
    )
    if type(page) is not int or page < 1:
        page = None
    bbox = value.get("bbox")
    if not isinstance(bbox, list):
        bbox = source.get("bbox")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(
            isinstance(number, (int, float))
            and not isinstance(number, bool)
            and math.isfinite(float(number))
            for number in bbox
        )
        or float(bbox[0]) >= float(bbox[2])
        or float(bbox[1]) >= float(bbox[3])
    ):
        bbox = None
    else:
        bbox = [float(number) for number in bbox]
    frame = (
        value.get("bbox_frame")
        or source.get("bbox_frame")
        or ("pdf_points" if bbox else None)
    )
    if frame != "pdf_points":
        bbox = None
        frame = None
    return {
        "source_document": source_document,
        "page": page,
        "bbox_frame": frame if frame == "pdf_points" else None,
        "bbox": bbox,
    }


def _pdf_review_geometry_target(
    jdir: Path,
    difference_id: str,
    target_path: list,
    context: Optional[_PdfReviewValidationContext] = None,
) -> dict:
    """Resolve a bounded target path against the hashed raw prediction."""
    raw = _pdf_review_raw_reaction(jdir, difference_id, context)
    if raw is None:
        raise HTTPException(400, "geometry correction reaction is unavailable")
    anchor, row, anchor_index, reaction_index = raw
    source_document = _runtime_pdf_anchor_source(anchor)
    head = target_path[0] if target_path else None
    target = None
    target_type = None
    target_id = None
    label = ""
    if (
        head in ("reactants", "products")
        and len(target_path) == 2
        and type(target_path[1]) is int
    ):
        values = row.get(head) if isinstance(row.get(head), list) else []
        index = target_path[1]
        if index >= len(values) or not isinstance(values[index], dict):
            raise HTTPException(400, "geometry molecule target is unavailable")
        target = values[index]
        role = "product" if head == "products" else "reactant"
        target_id = (
            f"{source_document}:a{anchor_index}:r{reaction_index}:{role}:{index}"
        )
        target_type = "molecule"
        label = str(target.get("identifier") or f"{head[:-1]} {index + 1}")
    elif (
        head == "conditions"
        and len(target_path) == 3
        and type(target_path[1]) is int
        and type(target_path[2]) is int
    ):
        steps = row.get("conditions") if isinstance(row.get("conditions"), list) else []
        step_index, item_index = target_path[1:]
        if step_index >= len(steps):
            raise HTTPException(400, "geometry condition step is unavailable")
        step = steps[step_index] if isinstance(steps[step_index], list) else [steps[step_index]]
        if item_index >= len(step) or not isinstance(step[item_index], dict):
            raise HTTPException(400, "geometry condition target is unavailable")
        target = step[item_index]
        target_id = f"{difference_id}:conditions:{step_index}:{item_index}"
        target_type = "condition"
        label = ": ".join(
            part
            for part in (
                str(target.get("role") or "condition"),
                str(target.get("text") or target.get("description") or ""),
            )
            if part
        )
    elif head == "metrics" and len(target_path) == 2 and type(target_path[1]) is int:
        values = row.get("metrics") if isinstance(row.get("metrics"), list) else []
        index = target_path[1]
        if index >= len(values) or not isinstance(values[index], dict):
            raise HTTPException(400, "geometry metric target is unavailable")
        target = values[index]
        target_id = f"{difference_id}:metrics:{index}"
        target_type = "metric"
        label = ": ".join(
            part
            for part in (
                str(target.get("role") or target.get("type") or "metric"),
                str(target.get("text") or target.get("content") or target.get("value") or ""),
            )
            if part
        )
    else:
        raise HTTPException(400, "geometry target_path is not an editable evidence field")
    original = _review_geometry_source(target, {
        "source_document": source_document,
    })
    if target_type == "molecule":
        side = "products" if head == "products" else "reactants"
        if context is not None and context.pdf_view is not None:
            view = context.pdf_view
        else:
            view = _read_json(jdir / "work" / "pdf_view.json") or {}
            if context is not None and isinstance(view, dict):
                context.pdf_view = view
        matches = [
            item
            for item in view.get("molecules") or []
            if isinstance(item, dict)
            and (
                "si"
                if (
                    item.get("source_document") == "si"
                    or item.get("src") == "si"
                )
                else "main"
            ) == source_document
            and item.get("anchor") == anchor_index
            and item.get("reaction") == reaction_index
            and item.get("side") == side
            and (
                item.get("mol_index") == target_path[1]
                or (
                    item.get("mol_index") is None
                    and target_path[1] == 0
                )
            )
        ]
        if len(matches) == 1:
            located = matches[0]
            original = _review_geometry_source({
                "bbox": located.get("bbox"),
                "bbox_page": located.get("page"),
                "bbox_frame": "pdf_points",
                "source": {"source_document": source_document},
            }, {"source_document": source_document})
        elif len(matches) > 1:
            raise HTTPException(409, "molecule PDF geometry is ambiguous")
    return {
        "target_type": target_type,
        "target_id": target_id,
        "target_path": target_path,
        "target_label": label[:320],
        "original": original,
    }


def _pdf_review_page_geometry(
    jdir: Path,
    source_document: str,
    page: int,
    context: Optional[_PdfReviewValidationContext] = None,
) -> tuple[float, float]:
    cache_key = (source_document, page)
    if context is not None and cache_key in context.page_dimensions:
        return context.page_dimensions[cache_key]
    if context is not None and context.pdf_view is not None:
        view = context.pdf_view
    else:
        view = _read_json(jdir / "work" / "pdf_view.json")
        if context is not None and isinstance(view, dict):
            context.pdf_view = view
    if (
        not isinstance(view, dict)
        or view.get("coordinate_space") not in (None, "pdf_points_v1")
    ):
        raise HTTPException(409, "PDF geometry is not available in pdf_points")
    if context is not None and context.document_catalog is not None:
        catalog = context.document_catalog
    else:
        catalog = _pdf_document_catalog(
            jdir,
            view,
            pdf_page_cache=(context.pdf_pages if context is not None else None),
        )
        if context is not None:
            context.document_catalog = catalog
    document = next(
        (
            item
            for item in catalog
            if isinstance(item, dict)
            and item.get("source") == source_document
        ),
        None,
    )
    if not isinstance(document, dict):
        raise HTTPException(400, "geometry destination document is unavailable")
    match = next(
        (
            item
            for item in document.get("pages") or []
            if isinstance(item, dict) and item.get("page") == page
        ),
        None,
    )
    if not isinstance(match, dict):
        raise HTTPException(400, "geometry destination page is unavailable")
    width, height = match.get("width"), match.get("height")
    if not all(
        isinstance(number, (int, float))
        and not isinstance(number, bool)
        and math.isfinite(float(number))
        and float(number) > 0
        for number in (width, height)
    ):
        raise HTTPException(409, "geometry destination page dimensions are unavailable")
    sealed_width, sealed_height = float(width), float(height)
    frame = document.get("coordinateFrame") or "pdf_points"
    if frame == "pdf_points":
        scale = 1.0
    elif frame == "page_render_px":
        dpi = document.get("coordinateDpi")
        if (
            not isinstance(dpi, (int, float))
            or isinstance(dpi, bool)
            or not math.isfinite(float(dpi))
            or float(dpi) <= 0
        ):
            raise HTTPException(409, "render-pixel document DPI is unavailable")
        scale = float(dpi) / 72
    else:
        raise HTTPException(409, "unsupported PDF document coordinate frame")
    if context is not None and source_document in context.pdf_pages:
        pdf_pages = context.pdf_pages[source_document]
    else:
        pdf_pages = _pdf_page_geometry(_pdf_source_path(jdir, source_document))
        if context is not None:
            context.pdf_pages[source_document] = pdf_pages
    actual = next(
        (item for item in pdf_pages if item.get("page") == page),
        None,
    )
    if not isinstance(actual, dict):
        raise HTTPException(409, "geometry destination PDF page is unavailable")
    actual_width = float(actual.get("width") or 0)
    actual_height = float(actual.get("height") or 0)
    expected_width = actual_width * scale
    expected_height = actual_height * scale
    tolerance = max(0.5, expected_width * 0.001, expected_height * 0.001)
    if (
        abs(expected_width - sealed_width) > tolerance
        or abs(expected_height - sealed_height) > tolerance
    ):
        raise HTTPException(
            409,
            "sealed pdf_view geometry does not match the attached PDF page",
        )
    dimensions = (actual_width, actual_height)
    if context is not None:
        context.page_dimensions[cache_key] = dimensions
    return dimensions


def _canonical_pdf_geometry_correction(
    jdir: Path,
    difference_id: str,
    value,
    context: Optional[_PdfReviewValidationContext] = None,
) -> dict:
    geometry = _clean_pdf_geometry_correction(value)
    target = _pdf_review_geometry_target(
        jdir,
        difference_id,
        geometry["target_path"],
        context,
    )
    for key in ("target_type", "target_id"):
        if geometry.get(key) != target[key]:
            raise HTTPException(
                400,
                f"geometry correction {key} does not match the current evidence target",
            )
    width, height = _pdf_review_page_geometry(
        jdir,
        geometry["source_document"],
        geometry["page"],
        context,
    )
    x1, y1, x2, y2 = geometry["bbox"]
    if x1 < 0 or y1 < 0 or x2 > width or y2 > height:
        raise HTTPException(400, "geometry bbox falls outside the destination page")
    original = target["original"]
    return {
        "schema": "chemrxn.pdf-geometry-correction.v1",
        "occurrence_id": target["target_id"],
        "reaction_id": difference_id,
        "target_type": target["target_type"],
        "target_path": target["target_path"],
        "target_label": target["target_label"],
        "original": original,
        "geometry": {
            "source_document": geometry["source_document"],
            "page": geometry["page"],
            "bbox_frame": geometry["bbox_frame"],
            "coordinate_space": "pdf_points_v1",
            "bbox": geometry["bbox"],
            "geometry_source": "human_review",
        },
    }


def _esmiles_extension_tag_counts(value: str) -> dict[str, int]:
    text = str(value or "")
    extension = text.split("<sep>", 1)[1] if "<sep>" in text else ""
    counts: dict[str, int] = {}
    for tag in re.findall(r"<([A-Za-z][A-Za-z0-9_-]*)\b[^>]*>", extension):
        name = tag.lower()
        if name == "sep":
            continue
        counts[name] = counts.get(name, 0) + 1
    return counts


def _esmiles_extension_semantics(value: str) -> list[str]:
    text = str(value or "")
    extension = text.split("<sep>", 1)[1] if "<sep>" in text else ""
    tokens = []
    for tag in ("a", "d", "r", "c", "v", "g", "s"):
        pattern = rf"<{tag}\b[^>]*>(.*?)</{tag}\s*>"
        for match in re.finditer(pattern, extension, flags=re.I | re.S):
            payload = match.group(1)
            semantic = ""
            if tag in ("a", "d", "c"):
                semantic = payload.split(":", 1)[1] if ":" in payload else payload
            elif tag == "r" and "<" not in payload:
                semantic = payload.split(":", 1)[1] if ":" in payload else payload
            elif tag == "v":
                virtual = re.fullmatch(
                    r"\s*\d+:([^:]+):\[(\d+):(\d+)\]\s*",
                    payload,
                )
                if virtual:
                    endpoints = sorted((int(virtual.group(2)), int(virtual.group(3))))
                    semantic = (
                        f"{virtual.group(1).strip()}:[{endpoints[0]}:{endpoints[1]}]"
                    )
                else:
                    semantic = payload.strip()
            elif tag == "g":
                semantic = re.sub(r"\s+", "", payload)
            elif tag == "s" or (tag == "r" and "<" in payload):
                # Nested E-SMILES has a separate coordinate space. Preserve it
                # exactly until a recursive atom mapper is available.
                semantic = payload.strip()
            if semantic:
                tokens.append(f"{tag}:{semantic}")
    return sorted(tokens)


def _esmiles_unknown_blocks(value: str) -> list[str]:
    text = str(value or "")
    extension = text.split("<sep>", 1)[1] if "<sep>" in text else ""
    supported = {"a", "d", "r", "c", "v", "g", "s"}
    blocks = []
    for match in re.finditer(
        r"<([A-Za-z][A-Za-z0-9_-]*)\b[^>]*>(.*?)</\1\s*>",
        extension,
        flags=re.S,
    ):
        if match.group(1).lower() not in supported:
            blocks.append(match.group(0).strip())
    return sorted(blocks)


def _esmiles_unknown_tags(value: str) -> list[str]:
    text = str(value or "")
    extension = text.split("<sep>", 1)[1] if "<sep>" in text else ""
    supported = {"a", "d", "r", "c", "v", "g", "s"}
    inline = {"dum", "id"}
    return sorted({
        match.group(1).lower()
        for match in re.finditer(
            r"<([A-Za-z][A-Za-z0-9_-]*)\b[^>]*>",
            extension,
            flags=re.I,
        )
        if match.group(1).lower() not in supported | inline | {"sep"}
    })


def _validate_pdf_esmiles_structure(candidate: str) -> None:
    """Validate one reviewed E-SMILES through the vendored deterministic loader.

    Ketcher is the interactive source of the edit, but its browser output is
    still untrusted at the mutation endpoint. This check performs no OCSR or
    model call and never rewrites the reviewed string.
    """
    package_root = REPO / "third_party" / "molecular_assembly_v2" / "src"
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    try:
        from esmiles import (  # type: ignore
            atom_id_by_position,
            load_esmiles,
            parse_virtual_arc_tag,
            ring_atom_sets,
            validate_esmiles_line,
        )
    except Exception as exc:
        raise HTTPException(
            503,
            "reviewed E-SMILES validator is unavailable",
        ) from exc

    issues = validate_esmiles_line(candidate, prefer_indigo=False)
    if issues:
        raise HTTPException(
            400,
            f"reviewed E-SMILES failed structural validation: {issues[0]}",
        )
    loaded = load_esmiles(candidate, apply_dummy=False, prefer_indigo=False)
    if loaded is None:
        raise HTTPException(400, "reviewed E-SMILES body is not parseable")
    atom_count = len(loaded.atom_by_position)

    def atom_at(position: int, tag: str):
        if position < 0 or position >= atom_count:
            raise HTTPException(
                400,
                f"reviewed E-SMILES <{tag}> position {position} is out of range",
            )
        atom_index = atom_id_by_position(loaded.atom_by_position, position)
        if atom_index < 0 or atom_index >= loaded.mol.GetNumAtoms():
            raise HTTPException(
                400,
                f"reviewed E-SMILES <{tag}> position {position} is unmapped",
            )
        return loaded.mol.GetAtomWithIdx(atom_index)

    for tag in [*loaded.a_tags, *loaded.c_tags, *loaded.d_tags]:
        atom = atom_at(tag.position, tag.kind)
        if tag.kind in ("a", "d") and atom.GetAtomicNum() != 0:
            raise HTTPException(
                400,
                f"reviewed E-SMILES <{tag.kind}> position {tag.position} is not a dummy atom",
            )

    for tag in loaded.v_tags:
        arc = parse_virtual_arc_tag(tag.position, tag.raw_value or tag.value)
        if arc is None:
            raise HTTPException(400, "reviewed E-SMILES contains an invalid <v> virtual arc")
        atom_at(arc.from_position, "v")
        atom_at(arc.to_position, "v")

    ring_count = len(ring_atom_sets(loaded.mol))
    contract = loaded.attachment_contract
    for ring in (contract.ring_substituents if contract else []):
        if ring.ring_index < 0 or ring.ring_index >= ring_count:
            raise HTTPException(
                400,
                f"reviewed E-SMILES <r> ring {ring.ring_index} is out of range",
            )

    for sgroup in loaded.sgroup_tags:
        for inner, outer in re.findall(r"\[(\d+):(\d+)\]", sgroup.value):
            atom_at(int(inner), "g")
            atom_at(int(outer), "g")


def _validate_pdf_molecule_correction(
    jdir: Path,
    difference_id: str,
    correction: dict,
    context: Optional[_PdfReviewValidationContext] = None,
) -> None:
    molecule_correction = (
        correction.get("molecule") if isinstance(correction, dict) else None
    )
    if not isinstance(molecule_correction, dict):
        return
    if context is not None and context.reviewer_evidence is not None:
        evidence = context.reviewer_evidence
    else:
        evidence = _read_json(jdir / "work" / "reviewer_v2.json")
        if context is not None and isinstance(evidence, dict):
            context.reviewer_evidence = evidence
    rows = evidence.get("rows") if isinstance(evidence, dict) else []
    row = next(
        (item for item in rows or []
         if isinstance(item, dict) and item.get("id") == difference_id),
        None,
    )
    if not isinstance(row, dict):
        raise HTTPException(400, "molecule correction reaction is unavailable")
    role = molecule_correction["role"]
    side = {"reactant": "reactants", "product": "products"}.get(role)
    if side is None:
        raise HTTPException(
            400,
            "condition molecule editing is not available in this reviewer",
        )
    index = molecule_correction["molecule_index"]
    molecules = row.get(side) if isinstance(row.get(side), list) else []
    if index >= len(molecules) or not isinstance(molecules[index], dict):
        raise HTTPException(400, "molecule correction index is unavailable")
    current = molecules[index]
    expected = current.get("occurrence")
    if not isinstance(expected, dict):
        raise HTTPException(
            409,
            "stable molecule occurrence identity is unavailable; re-import the job",
        )
    for key, expected_value in expected.items():
        if molecule_correction.get(key) != expected_value:
            raise HTTPException(
                400,
                f"molecule correction {key} does not match the current occurrence",
            )
    original = str(current.get("smi") or "")
    candidate = molecule_correction["esmiles"]
    _validate_pdf_esmiles_structure(candidate)
    original_extension = (
        original.split("<sep>", 1)[1].strip() if "<sep>" in original else ""
    )
    candidate_extension = candidate.split("<sep>", 1)[1].strip()
    if original_extension and not candidate_extension:
        raise HTTPException(400, "reviewed E-SMILES dropped a non-empty extension")
    if original_extension:
        expected_tags = _esmiles_extension_tag_counts(original)
        actual_tags = _esmiles_extension_tag_counts(candidate)
        if expected_tags != actual_tags:
            raise HTTPException(400, "reviewed E-SMILES changed extension categories")
        if (_esmiles_extension_semantics(original)
                != _esmiles_extension_semantics(candidate)):
            raise HTTPException(
                400,
                "reviewed E-SMILES changed extension labels or repeat semantics",
            )
        if _esmiles_unknown_blocks(original) != _esmiles_unknown_blocks(candidate):
            raise HTTPException(
                400,
                "reviewed E-SMILES changed unsupported extension metadata",
            )
        unknown_tags = _esmiles_unknown_tags(original)
        if unknown_tags and original_extension != candidate_extension:
            raise HTTPException(
                400,
                "reviewed E-SMILES must preserve unsupported extension metadata exactly",
            )


def _atomic_json(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def _exclusive_json(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError:
        raise HTTPException(409, "review version already exists") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


_PDF_REVIEW_LOCKS: dict[str, threading.RLock] = {}
_PDF_REVIEW_LOCKS_GUARD = threading.Lock()


@contextmanager
def _pdf_review_mutation_lock(jdir: Path):
    """Serialize one job's version/head mutation across threads and processes."""
    key = str(jdir.resolve())
    with _PDF_REVIEW_LOCKS_GUARD:
        process_lock = _PDF_REVIEW_LOCKS.setdefault(key, threading.RLock())
    with process_lock:
        lock_path = jdir / "work" / ".pdf-review.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as stream:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _pdf_review_proposal_digest(proposal: dict) -> str:
    unsigned = dict(proposal)
    unsigned.pop("proposal_id", None)
    return hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_pdf_review_delta_snapshot(
    jdir: Path,
    snapshot: dict,
    proposal: dict,
    version: int,
    context: _PdfReviewValidationContext,
) -> None:
    """Validate one constant-size parent+delta snapshot.

    Parent-state equality is checked during the single linear materialization
    pass; this function validates only the bounded child delta and immutable
    evidence identity.
    """
    if (
        snapshot.get("state_encoding") != "parent-plus-delta-v1"
        or "adjudications" in snapshot
        or "geometry_corrections" in snapshot
    ):
        raise HTTPException(409, f"reviewed-v{version} delta contract is invalid")
    rows = _pdf_review_rows(jdir, context)
    operation = snapshot.get("operation")
    reaction_id = snapshot.get("reaction_id")
    if (
        operation not in ("review_correction", "locate_pdf_geometry")
        or reaction_id not in rows
        or proposal.get("operation") != operation
        or proposal.get("reaction_id") != reaction_id
    ):
        raise HTTPException(409, f"reviewed-v{version} operation is invalid")
    if operation == "review_correction":
        try:
            after = _clean_product_review_item(proposal.get("after"))
        except (HTTPException, TypeError, ValueError) as exc:
            raise HTTPException(
                409,
                f"reviewed-v{version} review proposal is invalid",
            ) from exc
        if after != proposal.get("after") or after["difference_id"] != reaction_id:
            raise HTTPException(409, f"reviewed-v{version} review proposal is invalid")
        _validate_pdf_molecule_correction(
            jdir,
            reaction_id,
            after.get("correction") or {},
            context,
        )
        before = proposal.get("before")
        if before is not None:
            try:
                canonical_before, before_is_product = _clean_review_chain_adjudication(
                    before,
                )
            except (HTTPException, TypeError, ValueError) as exc:
                raise HTTPException(
                    409,
                    f"reviewed-v{version} review predecessor is invalid",
                ) from exc
            if (
                canonical_before != before
                or canonical_before["difference_id"] != reaction_id
                or (before_is_product and reaction_id not in rows)
            ):
                raise HTTPException(
                    409,
                    f"reviewed-v{version} review predecessor is invalid",
                )
        if (
            snapshot.get("verdict") != after["verdict"]
            or snapshot.get("correction") != after["correction"]
            or snapshot.get("note") != after["note"]
        ):
            raise HTTPException(409, f"reviewed-v{version} review proposal is invalid")
        return

    after_value = proposal.get("after")
    after_dict = after_value if isinstance(after_value, dict) else {}
    geometry = after_dict.get("geometry")
    try:
        after = {
            **_canonical_pdf_geometry_correction(
                jdir,
                reaction_id,
                {
                    **(geometry if isinstance(geometry, dict) else {}),
                    "target_type": after_dict.get("target_type"),
                    "target_id": after_dict.get("occurrence_id"),
                    "target_path": after_dict.get("target_path"),
                },
                context,
            ),
            "note": str(after_dict.get("note") or "")[:4000],
        }
    except (HTTPException, TypeError, ValueError) as exc:
        raise HTTPException(
            409,
            f"reviewed-v{version} geometry proposal is invalid",
        ) from exc
    if after != after_value:
        raise HTTPException(409, f"reviewed-v{version} geometry proposal is invalid")
    before = proposal.get("before")
    if before is not None:
        previous_geometry = before.get("geometry") if isinstance(before, dict) else None
        try:
            previous = {
                **_canonical_pdf_geometry_correction(
                    jdir,
                    str((before or {}).get("reaction_id") or ""),
                    {
                        **(
                            previous_geometry
                            if isinstance(previous_geometry, dict)
                            else {}
                        ),
                        "target_type": (before or {}).get("target_type"),
                        "target_id": (before or {}).get("occurrence_id"),
                        "target_path": (before or {}).get("target_path"),
                    },
                    context,
                ),
                "note": (before or {}).get("note"),
                "reviewed_version": (before or {}).get("reviewed_version"),
            }
        except (HTTPException, TypeError, ValueError) as exc:
            raise HTTPException(
                409,
                f"reviewed-v{version} geometry predecessor is invalid",
            ) from exc
        if (
            previous != before
            or type(previous["reviewed_version"]) is not int
            or not 1 <= previous["reviewed_version"] < version
        ):
            raise HTTPException(
                409,
                f"reviewed-v{version} geometry predecessor is invalid",
            )
    if (
        proposal.get("occurrence_id") != after["occurrence_id"]
        or snapshot.get("occurrence_id") != after["occurrence_id"]
        or snapshot.get("target_label") != after["target_label"]
        or snapshot.get("geometry") != after["geometry"]
        or snapshot.get("note") != after["note"]
    ):
        raise HTTPException(409, f"reviewed-v{version} geometry proposal is invalid")


def _validated_pdf_review_snapshot(
    jdir: Path,
    version: int,
    context: Optional[_PdfReviewValidationContext] = None,
) -> dict:
    """Read one immutable review snapshot and fail closed on any chain drift."""
    if (
        type(version) is not int
        or version < 0
        or version > _PDF_REVIEW_MAX_VERSIONS
    ):
        raise HTTPException(409, "PDF review snapshot version is invalid")
    context = context or _PdfReviewValidationContext(jdir)
    if version in context.validated_snapshots:
        return context.validated_snapshots[version]
    if version in context.validating_versions:
        raise HTTPException(409, "PDF review snapshot lineage contains a cycle")
    context.validating_versions.add(version)
    if version == 0:
        try:
            snapshot = _pdf_review_snapshot(jdir, 0, context)
            if snapshot is None:
                snapshot = (
                    _pdf_review_upgrade_baseline(jdir, context)
                    if _pdf_review_snapshot_files(jdir)
                    else _pdf_review_transient_baseline(jdir, context)
                )
                context.raw_snapshots[0] = snapshot
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("schema") != "chemrxn.pdf-review-baseline.v1"
                or snapshot.get("version") != 0
                or snapshot.get("parent_version") is not None
                or snapshot.get("geometry_corrections") != []
            ):
                raise HTTPException(409, "PDF review v0 baseline is invalid")
            _require_pdf_review_snapshot_source(jdir, snapshot, context)
            adjudications = snapshot.get("adjudications")
            if not isinstance(adjudications, list) or len(adjudications) > 2000:
                raise HTTPException(409, "PDF review v0 adjudications are invalid")
            seen_ids = set()
            rows = _pdf_review_rows(jdir, context)
            for item in adjudications:
                try:
                    canonical, is_product = _clean_review_chain_adjudication(item)
                except (HTTPException, TypeError, ValueError) as exc:
                    raise HTTPException(
                        409,
                        "PDF review v0 contains an invalid adjudication",
                    ) from exc
                difference_id = canonical["difference_id"]
                if (
                    canonical != item
                    or difference_id in seen_ids
                    or (is_product and difference_id not in rows)
                ):
                    raise HTTPException(
                        409,
                        "PDF review v0 adjudication identity is invalid",
                    )
                seen_ids.add(difference_id)
            digest = _pdf_review_baseline_digest(snapshot)
            expected_patch_id = (
                "sealed-prediction"
                if snapshot.get("migration") == "pre-baseline-version-chain"
                else f"sealed-prediction:{digest[:12]}"
            )
            if (
                snapshot.get("baseline_id") != digest
                or snapshot.get("patch_id") != expected_patch_id
                or snapshot.get("migration") not in (
                    None,
                    "pre-baseline-version-chain",
                )
            ):
                raise HTTPException(409, "PDF review v0 baseline digest is invalid")
            context.validated_snapshots[0] = snapshot
            return snapshot
        finally:
            context.validating_versions.discard(0)

    snapshot = _pdf_review_snapshot(jdir, version, context)
    if not isinstance(snapshot, dict):
        context.validating_versions.discard(version)
        raise HTTPException(409, f"reviewed-v{version} is unavailable")
    try:
        snapshot_schema = snapshot.get("schema")
        is_delta_snapshot = snapshot_schema == "chemrxn.pdf-reviewed-snapshot.v2"
        if (
            snapshot_schema not in (
                "chemrxn.pdf-reviewed-snapshot.v1",
                "chemrxn.pdf-reviewed-snapshot.v2",
            )
            or snapshot.get("version") != version
            or snapshot.get("raw_prediction_mutated") is not False
        ):
            raise HTTPException(409, f"reviewed-v{version} has an invalid snapshot contract")
        parent_version = snapshot.get("parent_version")
        if (
            type(parent_version) is not int
            or parent_version < 0
            or parent_version >= version
        ):
            raise HTTPException(409, f"reviewed-v{version} has an invalid parent")
        parent = _validated_pdf_review_snapshot(jdir, parent_version, context)
        current_source = _require_pdf_review_snapshot_source(jdir, snapshot, context)
        proposal = snapshot.get("proposal")
        if not isinstance(proposal, dict):
            raise HTTPException(409, f"reviewed-v{version} has no valid proposal")
        proposal_id = proposal.get("proposal_id")
        if (
            proposal.get("schema") != "chemrxn.proposed-review-patch.v1"
            or not isinstance(proposal_id, str)
            or not secrets.compare_digest(
                proposal_id,
                _pdf_review_proposal_digest(proposal),
            )
            or proposal.get("base_version") != parent_version
            or proposal.get("base_patch_id") != parent.get("patch_id")
            or proposal.get("source") != current_source
            or proposal.get("raw_prediction_mutated") is not False
        ):
            raise HTTPException(409, f"reviewed-v{version} proposal lineage is invalid")
        expected_patch_id = f"reviewed-v{version}:{proposal_id[:12]}"
        if (
            snapshot.get("patch_id") != expected_patch_id
            or snapshot.get("base_patch_id") != parent.get("patch_id")
        ):
            raise HTTPException(409, f"reviewed-v{version} patch lineage is invalid")
        diff = _read_json(jdir / "review" / f"diff-v{version}.json")
        if (
            not isinstance(diff, dict)
            or diff.get("schema") != "chemrxn.pdf-review-diff.v1"
            or diff.get("version") != version
            or diff.get("proposal") != proposal
        ):
            raise HTTPException(409, f"reviewed-v{version} diff lineage is invalid")

        if is_delta_snapshot:
            _validate_pdf_review_delta_snapshot(
                jdir,
                snapshot,
                proposal,
                version,
                context,
            )
            context.validated_snapshots[version] = snapshot
            return snapshot

        rows = _pdf_review_rows(jdir, context)
        adjudications = snapshot.get("adjudications")
        if not isinstance(adjudications, list) or len(adjudications) > 2000:
            raise HTTPException(409, f"reviewed-v{version} adjudications are invalid")
        canonical_adjudications: dict[str, dict] = {}
        for item in adjudications:
            try:
                canonical, is_product = _clean_review_chain_adjudication(item)
            except (HTTPException, TypeError, ValueError) as exc:
                raise HTTPException(
                    409,
                    f"reviewed-v{version} contains an invalid adjudication",
                ) from exc
            difference_id = canonical["difference_id"]
            if (
                difference_id in canonical_adjudications
                or canonical != item
                or (is_product and difference_id not in rows)
            ):
                raise HTTPException(
                    409,
                    f"reviewed-v{version} adjudication identity is invalid",
                )
            if is_product:
                _validate_pdf_molecule_correction(
                    jdir,
                    difference_id,
                    canonical.get("correction") or {},
                    context,
                )
            canonical_adjudications[difference_id] = canonical

        geometry_items = snapshot.get("geometry_corrections")
        if not isinstance(geometry_items, list) or len(geometry_items) > 10000:
            raise HTTPException(409, f"reviewed-v{version} geometry list is invalid")
        canonical_geometry: dict[str, dict] = {}
        for item in geometry_items:
            if not isinstance(item, dict):
                raise HTTPException(409, f"reviewed-v{version} geometry is invalid")
            reviewed_version = item.get("reviewed_version")
            note = item.get("note")
            if (
                type(reviewed_version) is not int
                or reviewed_version < 1
                or reviewed_version > version
                or not isinstance(note, str)
                or len(note) > 4000
            ):
                raise HTTPException(409, f"reviewed-v{version} geometry metadata is invalid")
            geometry = item.get("geometry")
            try:
                canonical = _canonical_pdf_geometry_correction(
                    jdir,
                    str(item.get("reaction_id") or ""),
                    {
                        **(geometry if isinstance(geometry, dict) else {}),
                        "target_type": item.get("target_type"),
                        "target_id": item.get("occurrence_id"),
                        "target_path": item.get("target_path"),
                    },
                    context,
                )
            except (HTTPException, TypeError, ValueError) as exc:
                raise HTTPException(
                    409,
                    f"reviewed-v{version} contains invalid PDF geometry",
                ) from exc
            expected_item = {
                **canonical,
                "note": note,
                "reviewed_version": reviewed_version,
            }
            occurrence_id = canonical["occurrence_id"]
            if occurrence_id in canonical_geometry or item != expected_item:
                raise HTTPException(
                    409,
                    f"reviewed-v{version} geometry identity is invalid",
                )
            canonical_geometry[occurrence_id] = expected_item

        operation = snapshot.get("operation")
        reaction_id = snapshot.get("reaction_id")
        if (
            operation not in ("review_correction", "locate_pdf_geometry")
            or reaction_id not in rows
            or proposal.get("operation") != operation
            or proposal.get("reaction_id") != reaction_id
        ):
            raise HTTPException(409, f"reviewed-v{version} operation is invalid")
        proposal_after = proposal.get("after")
        parent_adjudications = {
            item["difference_id"]: item
            for item in parent.get("adjudications") or []
            if isinstance(item, dict) and item.get("difference_id")
        }
        parent_geometry = {
            item["occurrence_id"]: item
            for item in parent.get("geometry_corrections") or []
            if isinstance(item, dict) and item.get("occurrence_id")
        }
        if operation == "locate_pdf_geometry":
            occurrence_id = snapshot.get("occurrence_id")
            active = canonical_geometry.get(occurrence_id)
            expected_geometry = dict(parent_geometry)
            if isinstance(active, dict):
                expected_geometry[occurrence_id] = active
            if (
                not isinstance(proposal_after, dict)
                or not isinstance(active, dict)
                or proposal.get("occurrence_id") != occurrence_id
                or proposal.get("before") != parent_geometry.get(occurrence_id)
                or snapshot.get("target_label") != proposal_after.get("target_label")
                or snapshot.get("geometry") != proposal_after.get("geometry")
                or snapshot.get("note") != proposal_after.get("note")
                or {
                    key: value
                    for key, value in active.items()
                    if key != "reviewed_version"
                } != proposal_after
                or active.get("reviewed_version") != version
                or canonical_adjudications != parent_adjudications
                or canonical_geometry != expected_geometry
            ):
                raise HTTPException(409, f"reviewed-v{version} geometry proposal is invalid")
        else:
            try:
                canonical_after = _clean_product_review_item(proposal_after)
            except (HTTPException, TypeError, ValueError) as exc:
                raise HTTPException(
                    409,
                    f"reviewed-v{version} review proposal is invalid",
                ) from exc
            active = canonical_adjudications.get(reaction_id)
            expected_adjudications = dict(parent_adjudications)
            expected_adjudications[reaction_id] = canonical_after
            if (
                canonical_after != proposal_after
                or proposal.get("before") != parent_adjudications.get(reaction_id)
                or active != proposal_after
                or snapshot.get("verdict") != proposal_after.get("verdict")
                or snapshot.get("correction") != proposal_after.get("correction")
                or snapshot.get("note") != proposal_after.get("note")
                or canonical_adjudications != expected_adjudications
                or canonical_geometry != parent_geometry
            ):
                raise HTTPException(409, f"reviewed-v{version} review proposal is invalid")
        context.validated_snapshots[version] = snapshot
        return snapshot
    finally:
        context.validating_versions.discard(version)


def _materialized_pdf_review_state(
    jdir: Path,
    version: int,
    context: Optional[_PdfReviewValidationContext] = None,
) -> dict:
    """Fold one active lineage once into the current human-review state."""
    context = context or _PdfReviewValidationContext(jdir)
    if (
        context.materialized_version == version
        and isinstance(context.materialized_state, dict)
    ):
        return context.materialized_state
    lineage = []
    cursor = version
    while cursor > 0:
        snapshot = _validated_pdf_review_snapshot(jdir, cursor, context)
        lineage.append(snapshot)
        cursor = snapshot.get("parent_version")
        if type(cursor) is not int or cursor < 0:
            raise HTTPException(409, "PDF review lineage cannot be materialized")
    baseline = _validated_pdf_review_snapshot(jdir, 0, context)
    adjudications = {
        item["difference_id"]: item
        for item in baseline.get("adjudications") or []
        if isinstance(item, dict) and item.get("difference_id")
    }
    geometry = {
        item["occurrence_id"]: item
        for item in baseline.get("geometry_corrections") or []
        if isinstance(item, dict) and item.get("occurrence_id")
    }
    for snapshot in reversed(lineage):
        if snapshot.get("schema") == "chemrxn.pdf-reviewed-snapshot.v1":
            # Compatibility checkpoint for immutable pre-delta history.  New
            # snapshots never repeat these cumulative arrays.
            adjudications = {
                item["difference_id"]: item
                for item in snapshot.get("adjudications") or []
                if isinstance(item, dict) and item.get("difference_id")
            }
            geometry = {
                item["occurrence_id"]: item
                for item in snapshot.get("geometry_corrections") or []
                if isinstance(item, dict) and item.get("occurrence_id")
            }
            continue
        proposal = snapshot["proposal"]
        if proposal["operation"] == "review_correction":
            key = proposal["reaction_id"]
            if proposal.get("before") != adjudications.get(key):
                raise HTTPException(
                    409,
                    f"reviewed-v{snapshot['version']} review delta does not match its parent",
                )
            adjudications[key] = proposal["after"]
        else:
            key = proposal["occurrence_id"]
            if proposal.get("before") != geometry.get(key):
                raise HTTPException(
                    409,
                    f"reviewed-v{snapshot['version']} geometry delta does not match its parent",
                )
            geometry[key] = {
                **proposal["after"],
                "reviewed_version": snapshot["version"],
            }
    state = {
        "adjudications": list(adjudications.values()),
        "geometry_corrections": list(geometry.values()),
    }
    context.materialized_version = version
    context.materialized_state = state
    return state


def _pdf_review_state(jdir: Path) -> dict:
    context = _PdfReviewValidationContext(jdir)
    head = _pdf_review_head(jdir)
    head_snapshot = _validated_pdf_review_snapshot(jdir, head, context)
    current_source = _require_pdf_review_snapshot_source(jdir, head_snapshot, context)
    materialized = _materialized_pdf_review_state(jdir, head, context)
    versions = []
    untrusted_history_versions = []
    for version, target in _pdf_review_snapshot_files(jdir):
        try:
            snapshot = _validated_pdf_review_snapshot(jdir, version, context)
        except HTTPException:
            if version == head:
                raise
            # Old branches may predate the current source-hash/snapshot
            # contract. They are neither deleted nor exported as trustworthy
            # history, and cannot become active without failing closed.
            untrusted_history_versions.append(version)
            continue
        versions.append({
            key: snapshot.get(key)
            for key in (
                "version", "patch_id", "parent_version", "reaction_id", "operation",
                "occurrence_id", "target_label", "geometry", "verdict", "correction",
                "note", "actor", "created_at",
                "base_patch_id", "raw_prediction_mutated",
            )
        })
    return {
        "schema": "chemrxn.pdf-review-state.v1",
        "head_version": head,
        "head_patch_id": head_snapshot.get("patch_id") if head_snapshot else "sealed-prediction",
        "versions": versions,
        "adjudications": materialized["adjudications"],
        "geometry_corrections": materialized["geometry_corrections"],
        "untrusted_history_versions": untrusted_history_versions,
        "source": current_source,
    }


def _figure_review_identity(meta: dict) -> tuple[str, str]:
    reviewer_url = meta.get("reviewer_url")
    fragment = urllib.parse.urlsplit(
        reviewer_url if isinstance(reviewer_url, str) else "",
    ).fragment
    match = re.fullmatch(
        r"/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)(?:[/?].*)?",
        fragment,
    )
    if not match:
        raise HTTPException(409, "Figure reviewer identity is unavailable")
    slug, task_id = match.groups()
    if slug in (".", "..") or task_id in (".", ".."):
        raise HTTPException(409, "Figure reviewer identity is invalid")
    return slug, task_id


def _review_export_payload(job_id: str, request: Request = None) -> tuple[dict, str]:
    """Build one honest review artifact without modifying or embedding GT.

    A Figure reviewed snapshot is a complete edited V2 record, so it is
    included as ``reviewed_prediction``. A PDF reviewed snapshot is a review
    sidecar (adjudications plus optional geometry corrections); it must not be
    presented as a newly extracted paper prediction until a separate
    materialization step exists.
    """
    if (
        not job_id
        or not all(character.isalnum() or character in "._-" for character in job_id)
        or job_id.startswith(".")
    ):
        raise HTTPException(404, "unknown job")
    jdir = JOBS_DIR / job_id
    meta = _read_json(jdir / "meta.json") or JOBS.get(job_id) or {}
    _require_job_owner(meta, request=request)
    raw_path = jdir / "result.json"
    if not raw_path.is_file():
        raise HTTPException(404, "no result yet")
    raw_bytes = raw_path.read_bytes()
    if b"GT_SENTINEL" in raw_bytes:
        raise HTTPException(409, "review export rejected: GT_SENTINEL was found in result.json")
    raw_prediction = _read_json(raw_path)
    _assert_gt_free(raw_prediction)

    if meta.get("mode") == "pdf":
        jdir, _ = _pdf_job(job_id)
        state = _pdf_review_state(jdir)
        adjudications = state["adjudications"]
        geometry_corrections = [
            item
            for item in state["geometry_corrections"]
            if isinstance(item, dict)
        ]
        if not state["head_version"]:
            legacy = _read_json(jdir / "review" / "adjudications.json")
            legacy_items = legacy.get("items") if isinstance(legacy, dict) else None
            if isinstance(legacy_items, list):
                adjudications = legacy_items
        adjudications = [item for item in adjudications if isinstance(item, dict)]
        if not state["head_version"] and not adjudications and not geometry_corrections:
            raise HTTPException(409, "No reviewed changes to export; raw prediction remains available")
        counts: dict[str, int] = {}
        for item in adjudications:
            verdict = item.get("verdict") or "unknown"
            counts[verdict] = counts.get(verdict, 0) + 1
        payload = {
            "schema": "chemrxn.review-export.v1",
            "target": "pdf",
            "job_id": job_id,
            "source": {
                **state["source"],
                "raw_prediction_immutable": True,
                "evaluation_artifacts_included": False,
            },
            "head_version": state["head_version"],
            "head_patch_id": state["head_patch_id"],
            "counts": counts,
            "geometry_count": len(geometry_corrections),
            "versions": state["versions"],
            "adjudications": adjudications,
            "geometry_corrections": geometry_corrections,
            "reviewed_prediction": None,
            "materialization": "review_sidecar_only",
        }
        rights = _export_source_rights(job_id, meta, raw_bytes)
        if rights:
            payload["source"]["rights"] = rights
        filename = f"{job_id}_reviewed_changes.json"
    else:
        slug, task_id = _figure_review_identity(meta)
        pointer = PDF_ROOT / slug / f"{task_id}_predicted_output.reviewed.json"
        reviewer_source = PDF_ROOT / slug / f"{task_id}_predicted_output.json"
        if not pointer.is_file():
            raise HTTPException(409, "No reviewed changes to export; raw prediction remains available")
        if not reviewer_source.is_file():
            raise HTTPException(409, "Original Figure reviewer source is unavailable")
        reviewed_bytes = pointer.read_bytes()
        reviewer_source_bytes = reviewer_source.read_bytes()
        if b"GT_SENTINEL" in reviewed_bytes:
            raise HTTPException(409, "review export rejected: GT_SENTINEL was found in reviewed snapshot")
        reviewed = _read_json(pointer)
        if not isinstance(reviewed, dict):
            raise HTTPException(409, "Current reviewed Figure snapshot is unreadable")
        _assert_gt_free(reviewed)
        reviewed_meta = reviewed.get("_reviewed_meta")
        reviewed_meta = reviewed_meta if isinstance(reviewed_meta, dict) else {}
        expected_source_hash = reviewed_meta.get("source_sha256")
        actual_source_hash = hashlib.sha256(reviewer_source_bytes).hexdigest()
        if expected_source_hash is not None and (
            not isinstance(expected_source_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_source_hash)
        ):
            raise HTTPException(409, "Reviewed Figure source hash is invalid")
        lineage_verified = isinstance(expected_source_hash, str)
        if lineage_verified and not secrets.compare_digest(expected_source_hash, actual_source_hash):
            raise HTTPException(409, "Reviewed Figure snapshot does not match its immutable source")
        payload = {
            "schema": "chemrxn.review-export.v1",
            "target": "figure",
            "job_id": job_id,
            "source": {
                "raw_result_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "reviewer_source_sha256": actual_source_hash,
                "reviewed_snapshot_sha256": hashlib.sha256(reviewed_bytes).hexdigest(),
                "review_lineage_verified": lineage_verified,
                "reviewer_identity": {"slug": slug, "task_id": task_id},
                "raw_prediction_immutable": True,
                "evaluation_artifacts_included": False,
            },
            "head_version": reviewed_meta.get("version"),
            "head_patch_id": reviewed_meta.get("proposal_id"),
            "reviewed_meta": reviewed_meta,
            "reviewed_prediction": reviewed,
            "materialization": (
                "complete_reviewed_figure_prediction"
                if lineage_verified
                else "legacy_reviewed_figure_prediction_unverified_source"
            ),
        }
        filename = f"{job_id}_reviewed_figure.json"
    _assert_gt_free(payload)
    return payload, filename


@app.get("/api/jobs/{job_id}/review/export")
def api_review_export(job_id: str, request: Request = None):
    """Export the active human-reviewed artifact; raw prediction stays immutable."""
    payload, filename = _review_export_payload(job_id, request=request)
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _pdf_geometry_location(record) -> Optional[dict]:
    if not isinstance(record, dict):
        return None
    value = record.get("geometry") if isinstance(record.get("geometry"), dict) else record
    bbox = value.get("bbox") if isinstance(value, dict) else None
    if not isinstance(bbox, list):
        return None
    return {
        "source_document": value.get("source_document"),
        "page": value.get("page"),
        "bbox_frame": value.get("bbox_frame"),
        "bbox": bbox,
    }


def _pdf_geometry_review_proposal(
    jdir: Path,
    body: dict,
    head: int,
    snapshot: dict,
    context: Optional[_PdfReviewValidationContext] = None,
) -> dict:
    """Build a canonical Locate-on-PDF proposal beside reaction adjudications."""
    supplied_after = body.get("after")
    if (
        isinstance(supplied_after, dict)
        and supplied_after.get("schema") == "chemrxn.pdf-geometry-correction.v1"
    ):
        target = {
            "difference_id": supplied_after.get("reaction_id"),
            "target_type": supplied_after.get("target_type"),
            "target_id": supplied_after.get("occurrence_id"),
            "target_path": supplied_after.get("target_path"),
        }
        geometry_in = {
            **(
                supplied_after.get("geometry")
                if isinstance(supplied_after.get("geometry"), dict)
                else {}
            ),
            "target_type": supplied_after.get("target_type"),
            "target_id": supplied_after.get("occurrence_id"),
            "target_path": supplied_after.get("target_path"),
        }
        note = supplied_after.get("note")
    else:
        target = body.get("target") if isinstance(body.get("target"), dict) else {}
        geometry_in = {
            **(supplied_after if isinstance(supplied_after, dict) else {}),
            "target_type": target.get("target_type"),
            "target_id": target.get("target_id"),
            "target_path": target.get("target_path"),
        }
        note = body.get("note")
    difference_id = str(target.get("difference_id") or "").strip()
    if difference_id not in _pdf_review_rows(jdir, context):
        raise HTTPException(400, "geometry correction does not identify a current review row")
    after = _canonical_pdf_geometry_correction(
        jdir,
        difference_id,
        geometry_in,
        context,
    )
    after["note"] = str(note or "")[:4000]
    active = {
        item.get("occurrence_id"): item
        for item in snapshot.get("geometry_corrections") or []
        if isinstance(item, dict) and item.get("occurrence_id")
    }
    before = active.get(after["occurrence_id"])
    before_location = (
        _pdf_geometry_location(before)
        if before
        else _pdf_geometry_location(after.get("original"))
    )
    if before_location == _pdf_geometry_location(after):
        raise HTTPException(409, "geometry correction does not change the evidence location")
    proposal = {
        "schema": "chemrxn.proposed-review-patch.v1",
        "reaction_id": difference_id,
        "occurrence_id": after["occurrence_id"],
        "base_version": head,
        "base_patch_id": snapshot.get("patch_id") or "sealed-prediction",
        "source": _pdf_review_source_hashes(jdir, context),
        "operation": "locate_pdf_geometry",
        "target": {
            key: after[key]
            for key in ("reaction_id", "occurrence_id", "target_type", "target_path", "target_label")
        },
        "before": before,
        "after": after,
        "proposed_by": "local reviewer",
        "proposed_at": str(
            body.get("proposed_at")
            or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ),
        "raw_prediction_mutated": False,
    }
    proposal["proposal_id"] = hashlib.sha256(
        json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return proposal


def _pdf_review_proposal(
    jdir: Path,
    body,
    context: Optional[_PdfReviewValidationContext] = None,
) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(400, "review proposal must be an object")
    base_version = body.get("base_version")
    if not isinstance(base_version, int) or base_version < 0:
        raise HTTPException(400, "base_version must be a non-negative integer")
    head = _pdf_review_head(jdir)
    if base_version != head:
        raise HTTPException(409, "review head changed; reload before proposing a correction")
    context = context or _PdfReviewValidationContext(jdir)
    snapshot = _validated_pdf_review_snapshot(jdir, head, context)
    _require_pdf_review_snapshot_source(jdir, snapshot, context)
    materialized = _materialized_pdf_review_state(jdir, head, context)
    if body.get("operation") == "locate_pdf_geometry":
        return _pdf_geometry_review_proposal(
            jdir,
            body,
            head,
            {**snapshot, **materialized},
            context,
        )
    after = _clean_product_review_item(body.get("after"))
    if after["difference_id"] not in _pdf_review_rows(jdir, context):
        raise HTTPException(400, "review correction does not identify a current review row")
    _validate_pdf_molecule_correction(
        jdir,
        after["difference_id"],
        after.get("correction") or {},
        context,
    )
    adjudications = {
        item.get("difference_id"): item
        for item in materialized["adjudications"]
        if isinstance(item, dict) and item.get("difference_id")
    }
    before = adjudications.get(after["difference_id"])
    comparable = lambda item: {
        "verdict": (item or {}).get("verdict") or "",
        "note": (item or {}).get("note") or "",
        "correction": (item or {}).get("correction") or None,
    }
    if comparable(before) == comparable(after):
        raise HTTPException(409, "no review fields changed")
    proposal = {
        "schema": "chemrxn.proposed-review-patch.v1",
        "reaction_id": after["difference_id"],
        "base_version": head,
        "base_patch_id": snapshot.get("patch_id") or "sealed-prediction",
        "source": _pdf_review_source_hashes(jdir, context),
        "operation": "review_correction",
        "before": before,
        "after": after,
        "proposed_by": "local reviewer",
        "proposed_at": str(body.get("proposed_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        "raw_prediction_mutated": False,
    }
    proposal["proposal_id"] = hashlib.sha256(
        json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return proposal


@app.get("/api/jobs/{job_id}/pdf/review/versions")
def api_pdf_review_versions(job_id: str, request: Request = None):
    jdir, _ = _pdf_job(job_id, request=request)
    return _pdf_review_state(jdir)


@app.post("/api/jobs/{job_id}/pdf/review/preview")
async def api_pdf_review_preview(job_id: str, request: Request):
    """Compute a product correction diff without writing any review or source file."""
    jdir, _ = _pdf_job(job_id, request=request)
    return _pdf_review_proposal(jdir, await request.json())


@app.post("/api/jobs/{job_id}/pdf/review/apply")
async def api_pdf_review_apply(job_id: str, request: Request):
    """Apply a revalidated proposal as an immutable reviewed-vN sidecar."""
    jdir, _ = _pdf_job(job_id, request=request)
    body = await request.json()
    supplied = body.get("proposal") if isinstance(body, dict) else None
    if not isinstance(supplied, dict):
        raise HTTPException(400, "proposal is required")
    agent_proposal_id = body.get("agent_proposal_id")
    if (
        agent_proposal_id is not None
        and (
            not isinstance(agent_proposal_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", agent_proposal_id)
        )
    ):
        raise HTTPException(400, "invalid agent proposal id")
    with _pdf_review_mutation_lock(jdir):
        context = _PdfReviewValidationContext(jdir)
        before_hashes = _pdf_review_source_hashes(jdir, context)
        proposal = _pdf_review_proposal(jdir, supplied, context)
        if supplied.get("proposal_id") != proposal["proposal_id"]:
            raise HTTPException(409, "proposal changed after preview")
        _ensure_pdf_review_baseline(jdir, context)
        current = _validated_pdf_review_snapshot(
            jdir,
            proposal["base_version"],
            context,
        )
        current_state = _materialized_pdf_review_state(
            jdir,
            proposal["base_version"],
            context,
        )
        adjudications = {
            item.get("difference_id"): item
            for item in current_state["adjudications"]
            if isinstance(item, dict) and item.get("difference_id")
        }
        geometry_corrections = {
            item.get("occurrence_id"): item
            for item in current_state["geometry_corrections"]
            if isinstance(item, dict) and item.get("occurrence_id")
        }
        existing = _pdf_review_snapshot_files(jdir)
        if len(existing) >= _PDF_REVIEW_MAX_VERSIONS:
            raise HTTPException(
                409,
                f"PDF review reached the {_PDF_REVIEW_MAX_VERSIONS}-version limit; export and start a new review cycle",
            )
        version = (existing[-1][0] if existing else 0) + 1
        review_dir = jdir / "review"
        snapshot_path = review_dir / f"reviewed-v{version}.json"
        diff_path = review_dir / f"diff-v{version}.json"
        # A process crash before the old implementation's snapshot publish
        # could leave a diff-only staging artifact. It never represented an
        # applied version and is safe to discard while holding the job lock.
        if diff_path.exists() and not snapshot_path.exists():
            diff_path.unlink()
        if proposal["operation"] == "locate_pdf_geometry":
            geometry_corrections[proposal["occurrence_id"]] = {
                **proposal["after"],
                "reviewed_version": version,
            }
        else:
            adjudications[proposal["reaction_id"]] = proposal["after"]
        snapshot = {
            "schema": "chemrxn.pdf-reviewed-snapshot.v2",
            "state_encoding": "parent-plus-delta-v1",
            "version": version,
            "patch_id": f"reviewed-v{version}:{proposal['proposal_id'][:12]}",
            "parent_version": proposal["base_version"],
            "reaction_id": proposal["reaction_id"],
            "operation": proposal["operation"],
            **(
                {
                    "occurrence_id": proposal["occurrence_id"],
                    "target_label": proposal["after"]["target_label"],
                    "geometry": proposal["after"]["geometry"],
                    "note": proposal["after"]["note"],
                }
                if proposal["operation"] == "locate_pdf_geometry"
                else {
                    "verdict": proposal["after"]["verdict"],
                    "correction": proposal["after"]["correction"],
                    "note": proposal["after"]["note"],
                }
            ),
            "actor": proposal["proposed_by"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "base_patch_id": proposal["base_patch_id"],
            "raw_prediction_mutated": False,
            "source": before_hashes,
            "proposal": proposal,
            **(
                {"agent_proposal_id": agent_proposal_id}
                if agent_proposal_id is not None else {}
            ),
        }
        if (
            _pdf_review_head(jdir) != proposal["base_version"]
            # This intentionally bypasses the request cache: a source change
            # after preview/validation must still abort the publish.
            or _pdf_review_source_hashes(jdir) != before_hashes
        ):
            raise HTTPException(409, "review head or immutable PDF source changed")
        diff_created = False
        snapshot_created = False
        head_published = False
        try:
            # Publish the diff first. The snapshot is the immutable version
            # commit record, so a failed second write cannot leave a version
            # visible to readers.
            _exclusive_json(diff_path, {
                "schema": "chemrxn.pdf-review-diff.v1",
                "version": version,
                "proposal": proposal,
            })
            diff_created = True
            _exclusive_json(snapshot_path, snapshot)
            snapshot_created = True
            if _pdf_review_head(jdir) != proposal["base_version"]:
                raise HTTPException(409, "review head changed before publish")
            _atomic_json(review_dir / "current.json", {
                "schema": "chemrxn.pdf-review-head.v1",
                "head_version": version,
                "updated_at": time.time(),
            })
            head_published = True
            _atomic_json(review_dir / "adjudications.json", {
                "schema": "pdf_adjudications_v1",
                "items": list(adjudications.values()),
            })
        except Exception:
            if not head_published:
                if snapshot_created and snapshot_path.exists():
                    snapshot_path.unlink()
                if diff_created and diff_path.exists():
                    diff_path.unlink()
            raise
        return _pdf_review_state(jdir)


@app.post("/api/jobs/{job_id}/pdf/review/undo")
async def api_pdf_review_undo(job_id: str, request: Request):
    """Move the active head to its immutable parent without deleting history."""
    jdir, _ = _pdf_job(job_id, request=request)
    body = await request.json()
    expected_head = body.get("head_version") if isinstance(body, dict) else None
    with _pdf_review_mutation_lock(jdir):
        context = _PdfReviewValidationContext(jdir)
        head = _pdf_review_head(jdir)
        if not isinstance(expected_head, int) or expected_head != head or head == 0:
            raise HTTPException(409, "review head changed or there is nothing to undo")
        snapshot = _validated_pdf_review_snapshot(jdir, head, context)
        parent = snapshot.get("parent_version")
        parent_snapshot = _validated_pdf_review_snapshot(jdir, parent, context)
        parent_state = _materialized_pdf_review_state(jdir, parent, context)
        _ensure_pdf_review_baseline(jdir, context)
        source_hashes = _pdf_review_source_hashes(jdir, context)
        if (
            _pdf_review_head(jdir) != expected_head
            or _pdf_review_source_hashes(jdir) != source_hashes
        ):
            raise HTTPException(409, "review head or immutable PDF source changed before undo")
        review_dir = jdir / "review"
        _atomic_json(review_dir / "current.json", {
            "schema": "chemrxn.pdf-review-head.v1",
            "head_version": parent,
            "updated_at": time.time(),
            "undo_of": head,
        })
        _atomic_json(review_dir / "adjudications.json", {
            "schema": "pdf_adjudications_v1",
            "items": parent_state["adjudications"],
        })
        return _pdf_review_state(jdir)


@app.get("/api/jobs/{job_id}/pdf/review/export")
def api_pdf_review_export(job_id: str, request: Request = None):
    """Export the legacy PDF review summary without GT-derived comparison data."""
    jdir, meta = _pdf_job(job_id, request=request)
    state = _pdf_review_state(jdir)
    if state["head_version"]:
        items = [
            item for item in state["adjudications"] if isinstance(item, dict)
        ]
    else:
        adjudications = (
            _read_json(jdir / "review" / "adjudications.json")
            or _empty_adjudications()
        )
        items = (
            [item for item in adjudications.get("items", []) if isinstance(item, dict)]
            if isinstance(adjudications, dict)
            else []
        )
    geometry_corrections = [
        item for item in state["geometry_corrections"] if isinstance(item, dict)
    ]
    counts: dict[str, int] = {}
    for item in items:
        verdict = item.get("verdict") or "unknown"
        counts[verdict] = counts.get(verdict, 0) + 1
    payload = {
        "schema": "pdf_review_summary_v1",
        "job_id": job_id,
        "paper": meta.get("filename"),
        "exported_at": time.time(),
        "counts": counts,
        "adjudications": items,
        "head_version": state["head_version"],
        "geometry_count": len(geometry_corrections),
        "geometry_corrections": geometry_corrections,
    }
    raw_path = jdir / "result.json"
    raw_bytes = raw_path.read_bytes() if raw_path.is_file() else None
    rights = (
        _export_source_rights(job_id, meta, raw_bytes)
        if raw_bytes is not None
        else None
    )
    if rights:
        payload["source_rights"] = rights
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="{job_id}_review_summary.json"'},
    )


def _pdf_source_path(jdir: Path, source: str) -> Path:
    if source not in ("main", "si"):
        raise HTTPException(400, "source must be main or si")
    path = jdir / "work" / f"{source}.pdf"
    if not path.exists():
        raise HTTPException(404, f"{source} PDF is not attached to this job")
    return path


def _pdf_page_geometry(path: Path, scale: float = 1.0) -> list[dict]:
    try:
        import fitz
        document = fitz.open(str(path))
        try:
            return [
                {
                    "page": index + 1,
                    "width": float(document.load_page(index).rect.width) * scale,
                    "height": float(document.load_page(index).rect.height) * scale,
                }
                for index in range(document.page_count)
            ]
        finally:
            document.close()
    except Exception:
        return []


def _pdf_document_catalog(
    jdir: Path,
    view: dict,
    pdf_page_cache: Optional[dict[str, list[dict]]] = None,
) -> list[dict]:
    def page_source(item: dict) -> str:
        value = (
            item.get("source_document")
            or item.get("src")
            or item.get("source")
        )
        return "si" if str(value or "main").lower() == "si" else "main"

    stored = _read_json(jdir / "work" / "documents.json") or {}
    records = stored.get("documents") if isinstance(stored.get("documents"), list) else []
    by_source = {
        item.get("source"): dict(item)
        for item in records
        if isinstance(item, dict) and item.get("source") in ("main", "si")
    }
    by_source.setdefault("main", {
        "source": "main",
        "pageCount": len([
            item for item in (view.get("pages") or [])
            if not isinstance(item, dict) or page_source(item) == "main"
        ]),
        "coordinateFrame": "pdf_points",
    })
    output = []
    for source in ("main", "si"):
        path = jdir / "work" / f"{source}.pdf"
        record = by_source.get(source)
        if record is None or not path.exists():
            continue
        source_pages = [
            dict(item)
            for item in (view.get("pages") or [])
            if isinstance(item, dict)
            and page_source(item) == source
        ]
        if source_pages:
            # A viewer-ready multi-document handoff may already carry exact
            # per-document PDF-point page geometry in one namespaced pages[].
            # Prefer those declared frames over re-deriving and scaling the SI
            # PDF. Legacy/runtime views contain only main pages and continue to
            # use the attached PDF geometry below.
            pages = source_pages
        elif source == "main":
            pages = [
                dict(item) for item in (view.get("pages") or [])
                if isinstance(item, dict)
            ]
        else:
            if pdf_page_cache is not None and source in pdf_page_cache:
                pages = pdf_page_cache[source]
            else:
                pages = _pdf_page_geometry(path)
                if pdf_page_cache is not None:
                    pdf_page_cache[source] = pages
        # The viewer overlay contract is canonical PDF points even when a
        # runtime's raw perception was generated from a 144-dpi page render.
        # Raw render-pixel lineage remains in perception; it is never reused as
        # the document overlay coordinate frame.
        record["coordinateFrame"] = "pdf_points"
        record["coordinateDpi"] = 72
        record["pages"] = pages
        record["pageCount"] = len(pages) or record.get("pageCount")
        record["available"] = True
        output.append(record)
    return output


@app.get("/api/jobs/{job_id}/pdf/file")
def api_pdf_file(job_id: str, source: str = "main", request: Request = None):
    jdir, _ = _pdf_job(job_id, request=request)
    path = _pdf_source_path(jdir, source)
    suffix = "_si" if source == "si" else ""
    return FileResponse(
        str(path),
        media_type="application/pdf",
        filename=f"{job_id}{suffix}.pdf",
    )


_PDF_DPI_MIN, _PDF_DPI_MAX = 36, 288


@app.get("/api/jobs/{job_id}/pdf/page/{page}")
def api_pdf_page(job_id: str, page: int, dpi: int = 144, source: str = "main",
                 request: Request = None):
    """Render ONE page lazily (PyMuPDF) into an atomic on-disk cache. `page` is 1-based; `dpi` is
    clamped to [36, 288]. Out-of-range pages 404 (never clamp a page — that would silently show the
    wrong page under an overlay)."""
    jdir, _ = _pdf_job(job_id, request=request)
    pdf = _pdf_source_path(jdir, source)
    try:
        dpi = int(dpi)
    except Exception:
        dpi = 144
    dpi = max(_PDF_DPI_MIN, min(dpi, _PDF_DPI_MAX))
    cache_name = (
        f"p{int(page):04d}_dpi{dpi}.png"
        if source == "main"
        else f"{source}_p{int(page):04d}_dpi{dpi}.png"
    )
    cache = jdir / "work" / "pdf_pages" / cache_name
    if not cache.exists():
        import fitz
        doc = fitz.open(str(pdf))
        try:
            if not isinstance(page, int) or page < 1 or page > doc.page_count:
                raise HTTPException(404, f"page out of range (1..{doc.page_count})")
            cache.parent.mkdir(parents=True, exist_ok=True)
            pix = doc.load_page(page - 1).get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                                                     alpha=False)
            # unique tmp + rename: concurrent first-requests must never publish a torn PNG
            # (explicit output= because pymupdf infers the format from the extension, and .tmp is not one)
            tmp = cache.parent / f".{cache.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
            pix.save(str(tmp), output="png")
            os.replace(tmp, cache)
            _prune_page_cache(cache.parent)
        finally:
            doc.close()
    # the render is deterministic per (job, page, dpi): let the browser keep it across restarts
    return FileResponse(str(cache), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


_PAGE_CACHE_MAX_FILES = int(os.environ.get("PDF_PAGE_CACHE_MAX", "120"))


def _prune_page_cache(cache_dir: Path) -> None:
    """Keep one per-job PDF image cache bounded (oldest-mtime first).

    Page and anchor crops use separate directories, so one shared limit and a generic PNG glob
    bounds both without allowing externally varied crop DPI/pad combinations to grow forever.
    """
    try:
        pngs = sorted(cache_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for p in pngs[:max(0, len(pngs) - _PAGE_CACHE_MAX_FILES)]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


@app.get("/api/jobs/{job_id}/pdf/anchor/{anchor_index}")
def api_pdf_anchor(job_id: str, anchor_index: int, request: Request = None):
    """One anchor's ORIGINAL prediction node (the chemistry source of truth, verbatim from
    result.json) + its view geometry + its molecule boxes."""
    jdir, _ = _pdf_job(job_id, request=request)
    pred = _read_json(jdir / "result.json")
    anchors = (pred or {}).get("anchors")
    if not isinstance(anchors, list):
        raise HTTPException(404, "no paper prediction stored")
    if not isinstance(anchor_index, int) or anchor_index < 0 or anchor_index >= len(anchors):
        raise HTTPException(404, f"anchor index out of range (0..{len(anchors) - 1})")
    view = _load_reconciled_pdf_view(jdir) or {}
    ventry = next((a for a in (view.get("anchors") or []) if a.get("index") == anchor_index), None)
    if ventry is not None:
        ventry = dict(ventry)
        ventry["has_reviewer"] = bool(ventry.get("reviewer_slug"))
    source_document = (
        ventry.get("source_document", "main") if isinstance(ventry, dict)
        else _runtime_pdf_anchor_source(anchors[anchor_index])
    )
    mols = [
        m for m in (view.get("molecules") or [])
        if m.get("anchor") == anchor_index
        and m.get("source_document", "main") == source_document
    ]
    claims = [
        c for c in (view.get("text_claims") or [])
        if c.get("anchor") == anchor_index
        and c.get("source_document", "main") == source_document
    ]
    return {"index": anchor_index, "anchor": anchors[anchor_index], "view": ventry,
            "molecules": mols, "text_claims": claims}


@app.get("/api/jobs/{job_id}/pdf/anchor/{anchor_index}/crop")
def api_pdf_anchor_crop(job_id: str, anchor_index: int, dpi: int = 144, pad: float = 8.0,
                        request: Request = None):
    """Render an existing pdf_view anchor bbox as a review-only crop.

    This is a geometry-preserving view over the stored PDF and pdf_view.json. It never changes
    prediction, perception, matching, GT, or the view index itself.
    """
    jdir, _ = _pdf_job(job_id, request=request)
    view = _load_reconciled_pdf_view(jdir) or {}
    anchor = next(
        (item for item in (view.get("anchors") or []) if item.get("index") == anchor_index),
        None,
    )
    bbox = anchor.get("bbox") if isinstance(anchor, dict) else None
    page = anchor.get("page") if isinstance(anchor, dict) else None
    source_document = anchor.get("source_document", "main") if isinstance(anchor, dict) else "main"
    if source_document not in ("main", "si"):
        raise HTTPException(409, "anchor source_document must be main or si")
    if not isinstance(bbox, list) or len(bbox) != 4 or not isinstance(page, int):
        raise HTTPException(404, "anchor crop geometry is unavailable")
    try:
        dpi = max(_PDF_DPI_MIN, min(int(dpi), _PDF_DPI_MAX))
        pad = max(0.0, min(float(pad), 36.0))
    except (TypeError, ValueError):
        raise HTTPException(400, "dpi and pad must be numeric")
    pdf = jdir / "work" / f"{source_document}.pdf"
    if not pdf.exists():
        raise HTTPException(404, "no PDF stored for this job")
    pad_key = int(round(pad * 10))
    pad = pad_key / 10.0
    cache = (
        jdir / "work" / "pdf_anchor_crops"
        / f"{source_document}_a{anchor_index:04d}_p{page:04d}_dpi{dpi}_pad{pad_key}.png"
    )
    if not cache.exists():
        import fitz
        doc = fitz.open(str(pdf))
        try:
            if page < 1 or page > doc.page_count:
                raise HTTPException(404, f"page out of range (1..{doc.page_count})")
            pdf_page = doc.load_page(page - 1)
            crop = fitz.Rect(
                float(bbox[0]) - pad,
                float(bbox[1]) - pad,
                float(bbox[2]) + pad,
                float(bbox[3]) + pad,
            ) & pdf_page.rect
            if crop.is_empty or crop.is_infinite:
                raise HTTPException(404, "anchor crop is empty")
            cache.parent.mkdir(parents=True, exist_ok=True)
            pix = pdf_page.get_pixmap(
                matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                clip=crop,
                alpha=False,
            )
            tmp = cache.parent / f".{cache.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
            pix.save(str(tmp), output="png")
            os.replace(tmp, cache)
            _prune_page_cache(cache.parent)
        finally:
            doc.close()
    return FileResponse(
        str(cache),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/jobs/{job_id}/detections")
def api_dets(job_id: str):
    data = _read_json(JOBS_DIR / job_id / "work" / "0_molparser.json")
    if data is None:
        raise HTTPException(404, "no detections")
    return data


@app.get("/api/jobs/{job_id}/mol/{idx}/crop")
def api_mol_crop(job_id: str, idx: int):
    """Crop the source figure to detection #idx's bbox (for the molecule QA tab)."""
    from PIL import Image
    work = JOBS_DIR / job_id / "work"
    dets = _read_json(work / "0_molparser.json")
    if dets is None or not (work / "0.png").exists():
        raise HTTPException(404, "no figure/detections")
    d = next((x for x in dets if isinstance(x, dict) and x.get("bbox_index") == idx), None)
    if not d or not d.get("bbox"):
        raise HTTPException(404, "no such detection bbox")
    x0, y0, x1, y1 = (int(v) for v in d["bbox"])
    im = Image.open(work / "0.png").convert("RGB")
    W, Hh, pad = im.size[0], im.size[1], 10
    crop = im.crop((max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(Hh, y1 + pad)))
    buf = io.BytesIO(); crop.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png")


@app.get("/api/jobs/{job_id}/molqa")
def api_molqa(job_id: str):
    """Per-detection molecule QA: join OCSR detections with the model's molecules by bbox_index,
    compute a CANONICAL match (RDKit), and tag origin. Works with no result yet (model_smi=null)."""
    jdir = JOBS_DIR / job_id
    dets = _read_json(jdir / "work" / "0_molparser.json") or []
    v2 = _read_json(jdir / "result.json")
    by_idx = {}
    if v2:
        for grp in (v2.get("selected_examples") or []) + (v2.get("reaction_templates") or []):
            for m in (grp.get("reactants") or []) + (grp.get("products") or []):
                s = m.get("_source") or {}
                bi = s.get("bbox_index")
                if bi is not None and bi not in by_idx:
                    by_idx[bi] = {"smi": (m.get("E-SMILES") or m.get("smiles") or "").split("<sep>")[0],
                                  "id": m.get("identifier"), "src": s}
    mols = []
    for d in dets:
        if not isinstance(d, dict):
            continue
        idx = d.get("bbox_index")
        mp = (d.get("smi") or "").split("<sep>")[0]
        mdl = by_idx.get(idx)
        model_smi = mdl["smi"] if mdl else None
        match = None
        if model_smi and mp:
            cm, cd = _canon(model_smi), _canon(mp)
            match = bool(cm and cm == cd)
        mols.append({"idx": idx, "score": d.get("score"), "mp_smi": mp, "model_smi": model_smi,
                     "identifier": (mdl["id"] if mdl else None), "used": bool(mdl), "match": match,
                     "origin": _mol_origin(mdl["src"] if mdl else {}, d)})
    return {"mols": mols, "has_result": v2 is not None}


@app.get("/api/jobs/{job_id}/scorecard")
def api_scorecard(job_id: str):
    """Compare GT vs molparser(OCSR) vs our agent — only for an uploaded benchmark figure with GT.
    Shows how many of GT's molecules raw OCSR found vs how many our agent got right (canonical sets)."""
    jdir = JOBS_DIR / job_id
    inp = next(iter(jdir.glob("input.*")), None)
    gt_path = _gt_for_upload(inp) if inp else None
    if not gt_path:
        raise HTTPException(404, "no ground truth for this figure (only benchmark figures have GT)")
    gt = _read_json(Path(gt_path))
    v2 = _read_json(jdir / "result.json")
    dets = _read_json(jdir / "work" / "0_molparser.json") or []
    if gt is None:
        raise HTTPException(404, "GT unreadable")

    g_all, g_prod, g_react = _v2_mols(gt), _v2_mols(gt, "products"), _v2_mols(gt, "reactants")
    a_all, a_prod, a_react = (_v2_mols(v2), _v2_mols(v2, "products"), _v2_mols(v2, "reactants")) if v2 else (set(), set(), set())
    m_all = _det_mols(dets)                       # OCSR has no role, so compare on the whole set / products
    b_all = set()                                 # baseline (Gemini, no tools) — only benchmark figures have it
    try:
        gp_ = Path(gt_path); fig_ = gp_.name.split("_")[0]
        bv = _read_json(REPO / "results" / "raw" / "pdf_bench" / "preds" / f"{gp_.parent.name}__{fig_}__baseline.json")
        if bv:
            b_all = _v2_mols(bv)
    except Exception:
        pass

    overall_r = None
    try:
        import score_v2 as SV
        if v2:
            overall_r = round(SV.score_v2(v2, gt).get("r"), 3)
    except Exception:
        pass

    # Reaction-LEVEL eval (grouped): aligns each reaction to GT and checks the whole tuple, so a reactant
    # missing from EVERY reaction craters the score (the set-based numbers above hide it). See scripts/reaction_eval.py.
    reaction_level = None
    self_check = None
    try:
        if str(REPO / "scripts") not in sys.path:
            sys.path.insert(0, str(REPO / "scripts"))
        import reaction_eval as RE
        import reaction_check as RC
        if v2:
            reaction_level = RE.reaction_score(v2, gt)
            self_check = RC.reaction_check(v2)            # GT-FREE template conformance (works without GT too)
    except Exception as e:
        reaction_level = {"applicable": False, "reason": f"{type(e).__name__}: {e}"}

    return {
        "has_gt": True,
        "gt_source": os.path.relpath(gt_path, REPO),
        "gt_counts": {"all": len(g_all), "products": len(g_prod), "reactants": len(g_react)},
        # MolParser is pure OCSR (no roles) -> precision/recall/F1 against ALL the figure's SMILES
        "molparser": _cmp(m_all, g_all),
        "baseline": (_cmp(b_all, g_all) if b_all else None),   # Gemini no-tools control (benchmark figs only)
        # our agent assigns roles -> compare on ALL (apples-to-apples) plus a per-role breakdown
        "agent": {"all": _cmp(a_all, g_all), "products": _cmp(a_prod, g_prod),
                  "reactants": _cmp(a_react, g_react)},
        "overall_r": overall_r,
        "reaction_level": reaction_level,                    # grouped per-reaction eval (catches systematic drops)
        "self_check": self_check,                            # GT-free template-conformance (cells off the 通式)
        "missed_by_molparser": sorted(g_all - m_all)[:40],   # GT molecules OCSR failed to detect
        "missed_by_agent": sorted(g_all - a_all)[:40],       # GT molecules the agent did not output
        "extra_by_agent": sorted(a_all - g_all)[:40],        # agent molecules not in GT
        # ---- fields beyond SMILES (where are we lacking?) ----
        "conditions": (lambda gc, pc: {
            "gt": len(gc), "agent": len(pc), "correct": len(gc & pc),
            "recall": round(len(gc & pc) / len(gc), 3) if gc else None,
            "precision": round(len(gc & pc) / len(pc), 3) if pc else None,
            "missed": sorted(gc - pc)[:30],
        })(_cond_set(gt), _cond_set(v2) if v2 else set()),
        "yields": (lambda gy, py: {
            "gt": len(gy), "agent": len(py),
            "correct": sum(1 for k, v in gy.items() if py.get(k) == v),
            "value_wrong": sorted(k for k, v in gy.items() if k in py and py[k] != v)[:20],
            "missing": sorted(k for k in gy if k not in py)[:20],
            "recall": round(sum(1 for k, v in gy.items() if py.get(k) == v) / len(gy), 3) if gy else None,
        })(_yields(gt), _yields(v2) if v2 else {}),
    }


@app.get("/api/jobs/{job_id}/gt_compare")
def api_gt_compare(job_id: str):
    """Per-ITEM agent-vs-GT comparison for the lower half of the 'vs GT' tab: each agent reaction placed
    next to its aligned GT reaction (renderable molecule lists + conditions + metrics) with per-field diff,
    plus a molecule-level diff (GT found/missed, agent-extra, near-miss pairs). Reuses reaction_eval's
    alignment (gt_index/pred_index on each row) so this view and the aggregate scorecard agree.
    404 when the figure has no GT -> the whole 'vs GT' tab is hidden client-side."""
    jdir = JOBS_DIR / job_id
    inp = next(iter(jdir.glob("input.*")), None)
    gt_path = _gt_for_upload(inp) if inp else None
    if not gt_path:
        raise HTTPException(404, "no ground truth for this figure")
    gt = _read_json(Path(gt_path))
    v2 = _read_json(jdir / "result.json")
    if gt is None or v2 is None:
        raise HTTPException(404, "GT or result unreadable")
    # Scope-figure conditions are stated ONCE (on the template / model reaction) and inherited by every example;
    # backfill that single global set into per-example conditions so each lane shows the reagents — including a
    # ligand/catalyst DRAWN as a structure (E-SMILES), which otherwise sits only on the template and never renders.
    gt = _backfill_conditions_v2(gt)
    v2 = _backfill_conditions_v2(v2)
    if str(REPO / "scripts") not in sys.path:
        sys.path.insert(0, str(REPO / "scripts"))
    import reaction_eval as RE

    # figure_truth = the agent's vision read of each printed label (its own _label_pass), so a product GT
    # mislabels but the agent draws as in the IMAGE is credited (product_gt_suspect), not shown as an agent error.
    ftruth = ((v2.get("_meta") or {}).get("_figure_labels")) or None
    score = RE.reaction_score(v2, gt, figure_truth=ftruth)
    rows = (score or {}).get("rows") or []
    gt_rxns, pred_rxns = RE._reactions(gt), RE._reactions(v2)        # same deterministic order the scorer aligned on
    pred_list = "selected_examples" if (v2.get("selected_examples")) else "reaction_templates"

    def _disp(m):
        raw = (m.get("E-SMILES") or m.get("smiles") or "")
        head = raw.split("<sep>")[0]
        return {"smi": head, "identifier": m.get("identifier"), "role": m.get("role"),
                "canon": RE._desalt(raw)}

    def _lane(raw):
        return {"reactants": [_disp(m) for m in (raw.get("reactants") or []) if isinstance(m, dict)],
                "products": [_disp(m) for m in (raw.get("products") or []) if isinstance(m, dict)],
                "conditions": raw.get("conditions"), "metrics": raw.get("metrics")}

    def _cset(lane, side):
        return {x["canon"] for x in lane[side] if x["canon"]}

    def _mark(lane, other, miss_label):
        for side in ("reactants", "products"):
            oset = other.get(side, set()) if other else set()
            for x in lane[side]:
                x["status"] = "match" if (x["canon"] and x["canon"] in oset) else miss_label

    out_reactions, aligned_pred = [], set()
    for row in rows:
        gi, pj = row.get("gt_index"), row.get("pred_index")
        if gi is None or gi >= len(gt_rxns):
            continue
        gt_lane = _lane(gt_rxns[gi]["raw"])
        agent_lane = None
        if pj is not None and pj < len(pred_rxns):
            aligned_pred.add(pj)
            agent_lane = _lane(pred_rxns[pj]["raw"])
        a_sets = {s: _cset(agent_lane, s) for s in ("reactants", "products")} if agent_lane else None
        g_sets = {s: _cset(gt_lane, s) for s in ("reactants", "products")}
        _mark(gt_lane, a_sets, "missing")
        if agent_lane:
            _mark(agent_lane, g_sets, "extra")
        gr, pr = g_sets["reactants"], (a_sets["reactants"] if a_sets else set())
        # missing conditions as DISPLAY text (row.conditions.missing are normalized keys; map back to GT texts)
        cond_missing = []
        rc = row.get("conditions")
        if rc and rc.get("missing"):
            mk = set(rc["missing"])
            cond_missing = [t for t in (rc.get("gt") or []) if RE._cond_key(t) in mk]
        out_reactions.append({
            "label": row.get("id"), "aligned": bool(agent_lane),
            "gt_suspect": bool(row.get("gt_suspect")), "product_gt_suspect": bool(row.get("product_gt_suspect")),
            "gt_markush_product": bool(row.get("gt_markush_product")), "failed": bool(row.get("failed")),
            "agent_gpath": [pred_list, pj] if pj is not None else None,
            "chips": {"reactants": f"{len(gr & pr)}/{len(gr)}", "product_ok": row.get("product_ok"),
                      "conditions_ok": row.get("conditions_ok"), "yield_ok": row.get("yield_ok")},
            "agent": agent_lane, "gt": gt_lane,
            "diff": {"product_ok": row.get("product_ok"), "conditions_missing": cond_missing,
                     "metrics": row.get("metrics")},
        })
    # agent reactions that aligned to NO GT reaction (the agent's extras)
    agent_only = []
    for pj, p in enumerate(pred_rxns):
        if pj in aligned_pred:
            continue
        agent_only.append({"label": p.get("label") or f"rxn{pj}", "agent_gpath": [pred_list, pj],
                           "agent": _lane(p["raw"])})

    # ---- molecule-level diff (GT found/missed, agent-extra, near-miss pairs) ----
    def _collect(src):
        seen = {}
        for grp in (src.get("selected_examples") or []) + (src.get("reaction_templates") or []):
            for role in ("reactants", "products"):
                for m in (grp.get(role) or []):
                    if not isinstance(m, dict):
                        continue
                    raw = (m.get("E-SMILES") or m.get("smiles") or "")
                    head = raw.split("<sep>")[0]
                    if "*" in head:
                        continue
                    c = RE._desalt(raw)
                    if c and c not in seen:
                        seen[c] = {"smi": head, "identifier": m.get("identifier"), "role": role, "canon": c}
        return seen
    gt_mols, agent_mols = _collect(gt), _collect(v2)
    gt_canon, agent_canon = set(gt_mols), set(agent_mols)
    gt_list = [{**v, "found": v["canon"] in agent_canon} for v in gt_mols.values()]
    agent_extra = [v for c, v in agent_mols.items() if c not in gt_canon]
    missed, extra = sorted(gt_canon - agent_canon), sorted(agent_canon - gt_canon)
    nearmiss = [{"gt_smi": g, "agent_smi": a} for g, a in RE._nearmiss_pairs(missed, extra)]

    return {
        "has_gt": True, "gt_source": os.path.relpath(gt_path, REPO),
        "reactions": out_reactions, "agent_only_reactions": agent_only,
        "molecules": {"gt": gt_list, "agent_extra": agent_extra, "nearmiss": nearmiss,
                      "counts": {"gt_found": sum(1 for x in gt_list if x["found"]),
                                 "gt_missed": len(missed), "agent_extra": len(agent_extra)}},
    }


def _rxn_label(g):
    """Title for a reaction in the Reactions tab. The agent often emits a GENERIC instance_id ('ex1','ex2')
    while the MEANINGFUL compound label sits on the product ('3a','3b'). Prefer a real instance_id, else the
    product identifier, else anything non-empty -- so the UI shows '3a', not 'ex1'."""
    import re
    iid = str(g.get("instance_id") or "").strip()
    if iid and not re.fullmatch(r"ex\d+", iid, re.I):
        return iid
    pid = next((str(p.get("identifier")).strip() for p in (g.get("products") or [])
                if isinstance(p, dict) and str(p.get("identifier") or "").strip()), "")
    return pid or str(g.get("identifier") or "").strip() or iid or ""


def _backfill_conditions_v2(v2):
    exs = v2.get("selected_examples") or []
    def ne(c):
        return bool(c) and any(s for s in c)
    sets = []
    for g in exs + (v2.get("reaction_templates") or []):
        c = g.get("conditions")
        if ne(c) and c not in sets:
            sets.append(c)
    if len(sets) == 1:
        for e in exs:
            if not ne(e.get("conditions")):
                e["conditions"] = sets[0]
    return v2


@app.get("/api/jobs/{job_id}/reactions")
def api_reactions(job_id: str):
    """Reaction-level view with HONEST provenance (origin = SMILES-match vs OCSR detections, not the
    model's bbox_index guess) and per-molecule GT match (canonical). Conditions are backfilled so
    every example is self-contained."""
    from tools_product.score_paper import norm_mol
    jdir = JOBS_DIR / job_id
    v2 = _read_json(jdir / "result.json")
    if v2 is None:
        raise HTTPException(404, "no result yet")
    v2 = _backfill_conditions_v2(v2)
    dets = _read_json(jdir / "work" / "0_molparser.json") or []
    det_canon = {norm_mol(d.get("smi") or "", False) for d in dets if isinstance(d, dict)}
    det_canon.discard(None)
    inp = next(iter(jdir.glob("input.*")), None)
    gt_path = _gt_for_upload(inp) if inp else None
    gt = _read_json(Path(gt_path)) if gt_path else None
    gt_canon = _v2_mols(gt) if gt else None

    # A molecule's "edited" flag + undo target are DERIVED from _manual_edits (no _source mutation), so undo that
    # pops the trail clears them automatically. Normalize a structure edit's audit path to the molecule path: a
    # /edit E-SMILES set records [...role,mi,"E-SMILES"] (len 5); an edit_smiles set records the molecule path (len 4).
    edits = v2.get("_manual_edits") or []
    orig_by_molpath = {}
    for e in edits:
        p = e.get("path")
        if not isinstance(p, list):
            continue
        molpath = None
        if len(p) == 5 and p[-1] == "E-SMILES" and p[2] in ("reactants", "products"):
            molpath = p[:-1]
        elif len(p) == 4 and p[2] in ("reactants", "products") and "from" in e and e.get("op") is None:
            molpath = p
        if molpath is not None:
            orig_by_molpath.setdefault(json.dumps(molpath), e.get("from"))

    def ann(m, path):
        raw = m.get("E-SMILES") or m.get("smiles") or ""
        c = norm_mol(raw, False)
        if c and "*" in c:
            origin = "template"                       # generic R-bearing structure
        elif c and c in det_canon:
            origin = "molparser"                      # OCSR actually read this exact structure
        else:
            origin = "agent"                          # model-built (reasoned/retro), no detection
        return {"identifier": m.get("identifier"), "smi": raw, "origin": origin,
                "gt_match": (None if gt_canon is None else bool(c and c in gt_canon)),
                # path addresses the on-disk V2 node for in-product editing; only selected_examples are editable
                # (templates are Markush). edited/orig drive the "edited" pill + undo; description/role are editable.
                "path": path, "edited": json.dumps(path) in orig_by_molpath,
                "orig": orig_by_molpath.get(json.dumps(path)),
                "description": m.get("description"), "role": m.get("role"),
                # a `*`-bearing molecule is an editable Markush scaffold (the UI shows a scaffold pencil on templates)
                "is_scaffold": bool(c and "*" in c)}

    def grp(g, is_t, list_name, gi):
        return {"label": "template" if is_t else _rxn_label(g),
                "is_template": is_t, "failed": g.get("failed"),
                # gpath addresses this group's on-disk V2 node; the client builds edit-op paths from it. Only
                # selected_examples are editable this round; the client renders template controls read-only.
                "gpath": [list_name, gi], "editable": (list_name == "selected_examples"),
                # Markush linkage: an example inheriting a template can have its R-groups edited -> re-enumerated.
                "inherit_template": g.get("inherit_template"), "markush_enumeration": g.get("markush_enumeration"),
                "reactants": [ann(m, [list_name, gi, "reactants", mi]) for mi, m in enumerate(g.get("reactants") or [])],
                "products": [ann(m, [list_name, gi, "products", mi]) for mi, m in enumerate(g.get("products") or [])],
                "conditions": g.get("conditions"), "metrics": g.get("metrics")}

    return {"has_gt": gt is not None, "n_edits": len(v2.get("_manual_edits") or []),
            "templates": [grp(t, True, "reaction_templates", i) for i, t in enumerate(v2.get("reaction_templates") or [])],
            "examples": [grp(e, False, "selected_examples", i) for i, e in enumerate(v2.get("selected_examples") or [])]}


# ─────────────────────────── in-product editing engine (the "Canvas") ───────────────────────────
# A user correcting a finished extraction edits result.json directly (NO agent re-run). All edits are surgical and
# autosaved one op at a time -- we never re-run assemble_and_validate (which would re-canon/reorder the whole doc).
# reaction_graph/topology is identifier-keyed, so structure/field edits leave it untouched; the molecule/reaction/GT
# views recompute from result.json on the next fetch. Mirrors the frozen reviewer's setAtPath conventions over the
# SAME V2 schema, but as validated server-side ops (set/append/delete) instead of a whole-doc deep-merge.
_EDITABLE_ROOTS = ("selected_examples", "reaction_templates")
_LEAF_KEYS = ("E-SMILES", "identifier", "description", "role", "text", "of", "note")


def _load_editable_job(job_id: str, request: Request):
    """Shared guard for the edit endpoints: finished job, exists, owned by the caller. Returns (result_path, v2)."""
    jdir = JOBS_DIR / job_id
    live = JOBS.get(job_id)
    if live and live.get("status") in ("queued", "running"):
        raise HTTPException(409, "job is still running; wait for it to finish before editing")
    res_path = jdir / "result.json"
    v2 = _read_json(res_path)
    if v2 is None:
        raise HTTPException(404, "no result to edit")
    meta = _read_json(jdir / "meta.json") or {}
    owner = meta.get("owner")
    if owner and owner != _identity(request):
        raise HTTPException(403, "not your job")
    return res_path, v2


def _persist_edit(res_path: Path, v2: dict, audit: list) -> None:
    """Append the audit trail + atomically rewrite result.json (same tmp+os.replace pattern as _save_meta)."""
    if audit:
        v2.setdefault("_manual_edits", []).extend(audit)
    tmp = res_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(v2))
    os.replace(tmp, res_path)


def _nav(v2, path):
    """Walk a V2 path, raising HTTPException(400) on any type/bounds mismatch. Returns the object at the path."""
    cur = v2
    for seg in path:
        if isinstance(seg, bool):                        # bool is an int subclass -- reject it explicitly
            raise HTTPException(400, "bad path segment")
        if isinstance(seg, int):
            if not isinstance(cur, list) or seg < 0 or seg >= len(cur):
                raise HTTPException(400, f"path index {seg} out of range")
            cur = cur[seg]
        elif isinstance(seg, str):
            if not isinstance(cur, dict) or seg not in cur:
                raise HTTPException(400, f"path key '{seg}' not found")
            cur = cur[seg]
        else:
            raise HTTPException(400, "bad path segment")
    return cur


def _canon_smiles_or_400(val):
    if not isinstance(val, str) or not val.strip():
        raise HTTPException(400, "empty SMILES")
    if "<sep>" in val or "*" in val:
        raise HTTPException(400, "multi-fragment / Markush (* or <sep>) editing is not supported yet")
    c = _canon(val)
    if not c:
        raise HTTPException(400, "unparseable SMILES")
    return c


def _blank_group(is_template):
    """A fresh empty example/template skeleton (mirrors the reviewer's TaskView addExample/addTemplate)."""
    base = {"reactants": [], "conditions": [[]], "products": [], "metrics": []}
    return ({"template_id": "T_new", **base} if is_template else {"instance_id": "new", **base})


def _mk_append_value(path, val):
    """Validate + normalize the value for an append op, by the array its path targets."""
    last = path[-1]
    if last in _EDITABLE_ROOTS:                           # appending a WHOLE example/template (top-level add)
        return _blank_group(last == "reaction_templates")
    if last == "conditions":                              # appending a whole STEP (list of condition entries)
        if not isinstance(val, list):
            raise HTTPException(400, "a condition step must be a list")
        return [{"role": str(c.get("role", "reagent")), "text": str(c.get("text", ""))}
                for c in val if isinstance(c, dict)]
    if last == "metrics":                                 # appending a metric
        if not isinstance(val, dict):
            raise HTTPException(400, "a metric must be an object")
        out = {"role": str(val.get("role", "yield")), "text": str(val.get("text", ""))}
        if val.get("of"):
            out["of"] = str(val["of"])
        return out
    if last in ("reactants", "products"):                 # appending a molecule
        if not isinstance(val, dict):
            raise HTTPException(400, "a molecule must be an object")
        return {"identifier": str(val.get("identifier", "")),
                "E-SMILES": _canon_smiles_or_400(val.get("E-SMILES")),
                "description": str(val.get("description", "")),
                "_source": {"origin": "user_edit", "manual": True}}
    if isinstance(last, int) and len(path) >= 2 and path[-2] == "conditions":   # appending a condition to a step
        if not isinstance(val, dict):
            raise HTTPException(400, "a condition must be an object")
        return {"role": str(val.get("role", "reagent")), "text": str(val.get("text", ""))}
    raise HTTPException(400, "cannot append at this path")


def _all_identifiers(v2):
    """Every molecule identifier still present across examples + templates (globally unique, shared intermediates)."""
    ids = set()
    for g in (v2.get("selected_examples") or []) + (v2.get("reaction_templates") or []):
        for m in (g.get("reactants") or []) + (g.get("products") or []):
            if isinstance(m, dict) and m.get("identifier"):
                ids.add(m["identifier"])
    return ids


def _prune_orphan_edges(v2):
    """Drop reaction_graph edges that reference an identifier no longer present (e.g. after a whole-example delete).
    A shared intermediate survives as long as ANY remaining example still carries its id. Returns the removed edges
    (so undo can restore them)."""
    rg = v2.get("reaction_graph")
    if not isinstance(rg, list):
        return []
    ids = _all_identifiers(v2)
    kept, removed = [], []
    for e in rg:
        if isinstance(e, dict) and all(m in ids for m in (e.get("from_mols") or []) + (e.get("to_mols") or [])):
            kept.append(e)
        else:
            removed.append(e)
    if removed:
        v2["reaction_graph"] = kept
    return removed


_MARKUSH_FNS = None
def _markush():
    """Import the Markush fill engine on demand (pure-RDKit, no network). Cached. Returns (forward_fill, is_markush)
    or None if unavailable -- so the re-enumerate feature degrades gracefully instead of 500ing."""
    global _MARKUSH_FNS
    if _MARKUSH_FNS is None:
        try:
            eng = str(REPO / "skills" / "chem_extract")
            if eng not in sys.path:
                sys.path.insert(0, eng)
            from engine.markush import forward_fill, is_markush  # noqa: E402
            _MARKUSH_FNS = (forward_fill, is_markush)
        except Exception:
            _MARKUSH_FNS = ()
    return _MARKUSH_FNS or None


def _annotate_markush(scaffold, labels):
    """forward_fill needs <a>atom_idx:label</a> tags to map an R-label onto a '*' position. Live extractions often
    emit a bare '*'-scaffold without them; infer the tags by pairing the '*' atoms (in index order) with the
    enumeration's labels (in order). Returns an annotated E-SMILES, or None when counts don't match / unparseable."""
    try:
        from rdkit import Chem, RDLogger
        RDLogger.DisableLog("rdApp.*")
    except Exception:
        return None
    bare = (scaffold or "").split("<sep>")[0]
    m = Chem.MolFromSmiles(bare)
    if m is None:
        return None
    stars = sorted(a.GetIdx() for a in m.GetAtoms() if a.GetAtomicNum() == 0)
    if not stars or len(stars) != len(labels):
        return None
    tags = "".join("<a>{}:{}</a>".format(i, lab) for i, lab in zip(stars, labels))
    return "{}<sep>{}".format(bare, tags)


def _apply_ops(v2, ops):
    """Apply a batch of edit ops to v2 in place. Returns audit entries. Raises only HTTPException(400)."""
    if not isinstance(ops, list) or not ops:
        raise HTTPException(400, "ops must be a non-empty list")
    if len(ops) > 50:
        raise HTTPException(400, "too many ops in one request")
    audit = []
    did_delete = False
    for op in ops:
        if not isinstance(op, dict):
            raise HTTPException(400, "each op must be an object")
        kind, path = op.get("op"), op.get("path")
        if not isinstance(path, list) or not path or path[0] not in _EDITABLE_ROOTS:
            raise HTTPException(400, "path must address a selected_examples/reaction_templates node")
        if len(path) == 1:                                # a top-level path supports only "add a whole reaction"
            if kind != "append":
                raise HTTPException(400, "a top-level path supports only append (add a whole reaction)")
        elif isinstance(path[1], bool) or not isinstance(path[1], int):
            raise HTTPException(400, "path must address a selected_examples/reaction_templates node")
        if kind == "set":
            key = path[-1]
            if len(path) < 3 or not isinstance(key, str) or key not in _LEAF_KEYS:
                raise HTTPException(400, "set path must end in an editable field")
            parent = _nav(v2, path[:-1])
            if not isinstance(parent, dict):
                raise HTTPException(400, "set target is not an object")
            old = parent.get(key)
            if key == "E-SMILES":
                c = _canon_smiles_or_400(op.get("value"))
                parent["E-SMILES"] = c
                if "smiles" in parent:
                    parent["smiles"] = c
                # NB: no _source mutation -- the "edited" flag is derived from _manual_edits so undo stays byte-exact
                audit.append({"op": "set", "path": path, "from": old, "to": c, "ts": time.time()})
            else:
                val = op.get("value")
                if val is None:
                    val = ""
                if not isinstance(val, (str, int, float)) or isinstance(val, bool):
                    raise HTTPException(400, "value must be a scalar")
                parent[key] = str(val)
                audit.append({"op": "set", "path": path, "from": old, "to": parent[key], "ts": time.time()})
        elif kind == "append":
            if len(path) == 1:                            # add a whole example/template at the top level
                if not isinstance(v2.get(path[0]), list):
                    v2[path[0]] = []
                target = v2[path[0]]
            else:
                target = _nav(v2, path)
                if not isinstance(target, list):
                    raise HTTPException(400, "append target is not a list")
            newv = _mk_append_value(path, op.get("value"))
            target.append(newv)
            # record the appended value so undo can delete it (and replay can re-add it)
            audit.append({"op": "append", "path": path + [len(target) - 1], "value": copy.deepcopy(newv), "ts": time.time()})
        elif kind == "delete":
            dkey = op.get("key")
            if dkey is not None:                          # drop a dict key (only 'of' -- clearing a metric target)
                if dkey != "of":
                    raise HTTPException(400, "only the 'of' key may be deleted")
                d = _nav(v2, path)
                if not isinstance(d, dict):
                    raise HTTPException(400, "delete-key target is not an object")
                removed = d.get("of")
                d.pop("of", None)
                audit.append({"op": "delete", "path": path, "key": "of", "value": removed, "ts": time.time()})
            else:                                         # remove an array element (path ends in its index)
                if isinstance(path[-1], bool) or not isinstance(path[-1], int):
                    raise HTTPException(400, "delete path must end in an array index")
                parent = _nav(v2, path[:-1])
                idx = path[-1]
                if not isinstance(parent, list) or idx < 0 or idx >= len(parent):
                    raise HTTPException(400, "delete index out of range")
                removed = copy.deepcopy(parent[idx])      # capture for undo (delete is now reversible)
                parent.pop(idx)
                # only a STRUCTURAL delete (a molecule, or a whole example/template) can orphan identifier-keyed
                # graph edges -> gate pruning on that, so a condition/metric delete never touches reaction_graph.
                if len(path) == 2 or path[-2] in ("reactants", "products"):
                    did_delete = True
                audit.append({"op": "delete", "path": path, "value": removed, "ts": time.time()})
        else:
            raise HTTPException(400, f"unknown op '{kind}'")
    if did_delete and isinstance(v2.get("reaction_graph"), list):
        pruned = _prune_orphan_edges(v2)                  # a delete may have orphaned identifier-keyed edges
        if pruned:
            audit.append({"op": "prune_edges", "edges": pruned, "ts": time.time()})
    return audit


@app.post("/api/jobs/{job_id}/edit")
async def api_edit(job_id: str, request: Request):
    """Generic surgical editor for a finished extraction: a batch of validated set/append/delete ops on
    conditions / metrics / molecule fields / reactant-product arrays. Autosaved one request at a time."""
    res_path, v2 = _load_editable_job(job_id, request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    audit = _apply_ops(v2, body.get("ops"))
    _persist_edit(res_path, v2, audit)
    return {"ok": True, "n_ops": len(audit)}


@app.post("/api/jobs/{job_id}/edit_smiles")
async def api_edit_smiles(job_id: str, request: Request):
    """Correct ONE concrete molecule's structure (the Ketcher pencil). A thin, dedicated wrapper over the same
    guards as /edit: it keeps the molecule-path audit + unchanged short-circuit + from/to return the UI relies on."""
    res_path, v2 = _load_editable_job(job_id, request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    path = body.get("path")
    smiles = body.get("smiles")
    # Validate the path shape: [list_name, group_index, role, mol_index] addressing a concrete molecule node.
    if (not isinstance(path, list) or len(path) != 4 or path[0] != "selected_examples"
            or path[2] not in ("reactants", "products")
            or isinstance(path[1], bool) or not isinstance(path[1], int)
            or isinstance(path[3], bool) or not isinstance(path[3], int)):
        raise HTTPException(400, "bad path (Phase 1 edits a selected_examples reactant/product only; templates are Markush)")
    cnew = _canon_smiles_or_400(smiles)
    mol = _nav(v2, path)
    if not isinstance(mol, dict):
        raise HTTPException(400, "target is not a molecule")
    old = mol.get("E-SMILES")
    if cnew == _canon(old or ""):
        return {"ok": True, "from": old, "to": cnew, "path": path, "unchanged": True}
    mol["E-SMILES"] = cnew
    if "smiles" in mol:                                  # keep a bare-smiles shadow consistent if present
        mol["smiles"] = cnew
    # NB: no _source mutation -- "edited" is derived from _manual_edits so undo stays byte-exact
    _persist_edit(res_path, v2, [{"path": path, "from": old, "to": cnew, "ts": time.time()}])
    return {"ok": True, "from": old, "to": cnew, "path": path}


def _inherited_template(v2, ex):
    """The template an example inherits: matched by template_id when inherit_template is a string id; otherwise (a
    truthy flag / the common single-template case) the lone template. None when there is nothing to inherit from."""
    tmpls = v2.get("reaction_templates") or []
    tid = ex.get("inherit_template")
    t = next((x for x in tmpls if x.get("template_id") == tid), None) if isinstance(tid, str) and tid else None
    if t is None and tid and len(tmpls) == 1:
        t = tmpls[0]
    return t


def _refill_example(ei, ex, tmpl, forward_fill, is_markush):
    """Re-derive example #ei's reactants AND products from the template's same-index `*`-scaffolds, using the
    example's markush_enumeration (forward_fill). Untagged scaffolds get `<a>` tags inferred via _annotate_markush.
    Mutates ex in place; returns (filled, audit) -- filled={"reactants":[smi...],"products":[smi...]}, audit a list
    of invertible set entries. A scaffold that isn't markush, or whose fill yields nothing, is left untouched."""
    enumeration = ex.get("markush_enumeration") or {}
    filled, audit = {"reactants": [], "products": []}, []
    for role in ("reactants", "products"):
        emols = ex.get(role) or []
        for k, tp in enumerate(tmpl.get(role) or []):
            scaffold = (tp.get("E-SMILES") or tp.get("smiles") or "") if isinstance(tp, dict) else ""
            if not scaffold or not is_markush(scaffold):
                continue
            sc = scaffold if "<a>" in scaffold else (_annotate_markush(scaffold, list(enumeration.keys())) or scaffold)
            try:
                new = forward_fill(sc, dict(enumeration))
            except Exception:
                new = None
            if new and k < len(emols) and isinstance(emols[k], dict):
                old = emols[k].get("E-SMILES")
                if new != old:
                    emols[k]["E-SMILES"] = new
                    src = emols[k].setdefault("_source", {}); src["origin"] = "markush"; src["manual"] = True
                    audit.append({"op": "set", "path": ["selected_examples", ei, role, k, "E-SMILES"],
                                  "from": old, "to": new, "ts": time.time()})
                filled[role].append(new)
    return filled, audit


@app.post("/api/jobs/{job_id}/reenumerate")
async def api_reenumerate(job_id: str, request: Request):
    """Markush re-enumeration: change ONE example's R-group assignment and re-derive its reactants + products from
    the inherited template scaffolds via forward_fill (pure-RDKit, no network). Records invertible set audits."""
    res_path, v2 = _load_editable_job(job_id, request)
    fns = _markush()
    if not fns:
        raise HTTPException(503, "markush engine unavailable")
    forward_fill, is_markush = fns
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    gpath, enum = body.get("gpath"), body.get("enumeration")
    if (not isinstance(gpath, list) or len(gpath) != 2 or gpath[0] != "selected_examples"
            or isinstance(gpath[1], bool) or not isinstance(gpath[1], int)):
        raise HTTPException(400, "gpath must be [selected_examples, i]")
    exs = v2.get("selected_examples") or []
    if gpath[1] < 0 or gpath[1] >= len(exs):
        raise HTTPException(400, "example index out of range")
    ex = exs[gpath[1]]
    audit = []
    if enum is not None:
        if not isinstance(enum, dict) or not enum or not all(
                isinstance(k, str) and isinstance(val, str) and val.strip() for k, val in enum.items()):
            raise HTTPException(400, "enumeration must be a non-empty {label: '*frag'} string map")
        old_enum = ex.get("markush_enumeration")
        ex["markush_enumeration"] = enum
        audit.append({"op": "set", "path": gpath + ["markush_enumeration"],
                      "from": old_enum, "to": copy.deepcopy(enum), "ts": time.time()})
    tmpl = _inherited_template(v2, ex)
    if not tmpl:
        if audit:
            _persist_edit(res_path, v2, audit)
        return {"ok": False, "reason": "no inherited template to enumerate from"}
    filled, fill_audit = _refill_example(gpath[1], ex, tmpl, forward_fill, is_markush)
    audit += fill_audit
    if not filled["products"] and not filled["reactants"] and not audit:
        return {"ok": False, "reason": "no markush scaffold matched / fill produced nothing"}
    _persist_edit(res_path, v2, audit)
    return {"ok": True, "products": filled["products"], "reactants": filled["reactants"],
            "enumeration": ex.get("markush_enumeration") or {}}


def _scaffold_labels(scaffold, inheriting_examples):
    """The R-group labels for a template scaffold: read its `<a>idx:label</a>` tags; if untagged, fall back to the
    union (first-seen order) of the inheriting examples' markush_enumeration keys."""
    import re
    labels = re.findall(r"<a>\d+:([^<]+)</a>", scaffold or "")
    if labels:
        return labels
    seen = []
    for ex in inheriting_examples:
        for k in (ex.get("markush_enumeration") or {}):
            if k not in seen:
                seen.append(k)
    return seen


@app.post("/api/jobs/{job_id}/edit_scaffold")
async def api_edit_scaffold(job_id: str, request: Request):
    """Edit a Markush template's `*`-scaffold STRUCTURE (text-SMILES; Ketcher can't round-trip `*`), regenerate its
    `<a>` label tags, then re-derive EVERY inheriting example's molecules via forward_fill. Invertible set audits."""
    res_path, v2 = _load_editable_job(job_id, request)
    fns = _markush()
    if not fns:
        raise HTTPException(503, "markush engine unavailable")
    forward_fill, is_markush = fns
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    tpath, smiles = body.get("tpath"), body.get("smiles")
    if (not isinstance(tpath, list) or len(tpath) != 4 or tpath[0] != "reaction_templates"
            or tpath[2] not in ("reactants", "products")
            or isinstance(tpath[1], bool) or not isinstance(tpath[1], int)
            or isinstance(tpath[3], bool) or not isinstance(tpath[3], int)):
        raise HTTPException(400, "tpath must be [reaction_templates, t, reactants|products, k]")
    if not isinstance(smiles, str) or not smiles.strip():
        raise HTTPException(400, "empty SMILES")
    bare = smiles.split("<sep>")[0].strip()
    if "*" not in bare:
        raise HTTPException(400, "a scaffold must contain at least one '*' R-group placeholder")
    tmpls = v2.get("reaction_templates") or []
    if tpath[1] < 0 or tpath[1] >= len(tmpls):
        raise HTTPException(400, "template index out of range")
    tmpl = tmpls[tpath[1]]
    arr = tmpl.get(tpath[2]) or []
    if tpath[3] < 0 or tpath[3] >= len(arr) or not isinstance(arr[tpath[3]], dict):
        raise HTTPException(400, "scaffold molecule not found")
    tmol = arr[tpath[3]]
    old_scaffold = tmol.get("E-SMILES")
    inheriting = [ex for ex in (v2.get("selected_examples") or []) if _inherited_template(v2, ex) is tmpl]
    labels = _scaffold_labels(old_scaffold or "", inheriting)
    if not labels:
        raise HTTPException(400, "cannot determine this scaffold's R-group labels (no tags, no inheriting examples)")
    new_scaffold = _annotate_markush(bare, labels)
    if not new_scaffold:
        raise HTTPException(400, "the edited scaffold must be parseable and keep the same number of R-groups "
                                 "({}: {})".format(len(labels), ", ".join(labels)))
    audit = []
    if new_scaffold != old_scaffold:
        tmol["E-SMILES"] = new_scaffold
        audit.append({"op": "set", "path": tpath + ["E-SMILES"], "from": old_scaffold, "to": new_scaffold, "ts": time.time()})
    n_ex = n_mol = 0
    for ei, ex in enumerate(v2.get("selected_examples") or []):
        if _inherited_template(v2, ex) is not tmpl:
            continue
        _, fa = _refill_example(ei, ex, tmpl, forward_fill, is_markush)
        if fa:
            n_ex += 1; n_mol += len(fa); audit += fa
    if not audit:
        return {"ok": False, "reason": "scaffold unchanged and no examples to refill"}
    _persist_edit(res_path, v2, audit)
    return {"ok": True, "refilled_examples": n_ex, "refilled_mols": n_mol, "scaffold": new_scaffold}


@app.get("/api/jobs/{job_id}/occurrences")
def api_occurrences(job_id: str, smi: str = ""):
    """Every editable molecule (selected_examples only) whose canonical E-SMILES matches `smi` -- powers the
    Molecules-tab 'fix this structure everywhere' chooser. Returns E-SMILES leaf paths for batch set ops."""
    v2 = _read_json(JOBS_DIR / job_id / "result.json")
    if v2 is None:
        raise HTTPException(404, "no result")
    target = _canon(smi)
    out = []
    if target:
        for i, ex in enumerate(v2.get("selected_examples") or []):
            for role in ("reactants", "products"):
                for j, m in enumerate(ex.get(role) or []):
                    if isinstance(m, dict) and _canon(m.get("E-SMILES") or m.get("smiles") or "") == target:
                        out.append({"path": ["selected_examples", i, role, j, "E-SMILES"],
                                    "identifier": m.get("identifier"), "role": role,
                                    "group": ex.get("instance_id") or ex.get("identifier") or f"example {i + 1}"})
    return {"occurrences": out, "canonical": target}


def _invert_entry(v2, e):
    """Apply the inverse of one audit entry in place. Best-effort (skips on a stale path). Returns True if applied."""
    op, path = e.get("op"), e.get("path")
    try:
        if op == "set":
            parent = _nav(v2, path[:-1]); key = path[-1]
            if e.get("from") is None:
                parent.pop(key, None)
            else:
                parent[key] = e["from"]
                if key == "E-SMILES" and isinstance(parent, dict) and "smiles" in parent:
                    parent["smiles"] = e["from"]
        elif op == "append":
            parent = _nav(v2, path[:-1]); idx = path[-1]
            if isinstance(parent, list) and 0 <= idx < len(parent):
                parent.pop(idx)
        elif op == "delete":
            if e.get("key") == "of":
                d = _nav(v2, path)
                if isinstance(d, dict) and e.get("value") is not None:
                    d["of"] = e["value"]
            else:
                parent = _nav(v2, path[:-1]); idx = path[-1]
                if isinstance(parent, list) and e.get("value") is not None:
                    parent.insert(min(idx, len(parent)), copy.deepcopy(e["value"]))
        elif op == "prune_edges":
            rg = v2.setdefault("reaction_graph", [])
            if isinstance(rg, list):
                rg.extend(e.get("edges") or [])
        elif op is None and "from" in e and isinstance(path, list):   # an edit_smiles entry (molecule E-SMILES)
            mol = _nav(v2, path)
            if isinstance(mol, dict):
                mol["E-SMILES"] = e.get("from")
                if "smiles" in mol:
                    mol["smiles"] = e.get("from")
        else:
            return False
        return True
    except HTTPException:
        return False


def _write_v2(res_path, v2):
    tmp = res_path.with_suffix(".json.tmp"); tmp.write_text(json.dumps(v2)); os.replace(tmp, res_path)


@app.post("/api/jobs/{job_id}/undo")
async def api_undo(job_id: str, request: Request):
    """Undo the LAST edit (sequential stack). Repeated calls walk back to any prior state."""
    res_path, v2 = _load_editable_job(job_id, request)
    edits = v2.get("_manual_edits") or []
    if not edits:
        raise HTTPException(400, "nothing to undo")
    e = edits[-1]
    _invert_entry(v2, e)
    edits.pop()
    v2["_manual_edits"] = edits
    _write_v2(res_path, v2)
    return {"ok": True, "undone": e.get("op") or "edit", "remaining": len(edits)}


@app.post("/api/jobs/{job_id}/revert_all")
async def api_revert_all(job_id: str, request: Request):
    """Revert every recorded edit (inverses applied newest-first), restoring the pre-edit document."""
    res_path, v2 = _load_editable_job(job_id, request)
    edits = v2.get("_manual_edits") or []
    n = len(edits)
    for e in reversed(edits):
        _invert_entry(v2, e)
    v2["_manual_edits"] = []
    _write_v2(res_path, v2)
    return {"ok": True, "reverted": n}


_RENDER_CACHE: dict = {}                     # (smi, w, h) -> svg; small in-process LRU
_RENDER_CACHE_MAX = 1024


def _render_svg_cached(smi: str, w: int, h: int) -> str:
    key = (smi, w, h)
    hit = _RENDER_CACHE.pop(key, None)
    if hit is not None:
        _RENDER_CACHE[key] = hit             # re-insert = most-recently-used
        return hit
    svg = _render_svg(smi, w, h)
    _RENDER_CACHE[key] = svg
    while len(_RENDER_CACHE) > _RENDER_CACHE_MAX:
        _RENDER_CACHE.pop(next(iter(_RENDER_CACHE)))
    return svg


@app.get("/api/render")
def api_render(smi: str = "", w: int = 220, h: int = 160):
    """Server-side RDKit render, LRU-cached per (SMILES,w,h) and browser-cacheable: the document
    view renders the same molecules on every anchor expand/compare toggle, so both caches matter."""
    w, h = max(60, min(w, 600)), max(60, min(h, 600))
    return Response(_render_svg_cached(smi, w, h), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# Small source-only set for the product home. Repository fixtures are joined with
# an optional coordinator-installed gallery. Gallery manifests may point only to
# files below CHEMRXN_PRODUCT_SAMPLE_ROOT and never expose GT, scores, or results.
EXAMPLE_SAMPLE_RIGHTS = {
    "status": "verified",
    "license": "cc-by-4.0",
    "label": "CC BY 4.0",
    "attribution": (
        "Aline Nunes De Souza et al., Development of a Scalable Synthetic Route "
        "to (1R,5R)-2,2-Dimethoxybicyclo[3.1.0]hexan-3-one, "
        "Org. Process Res. Dev. 2025, 29, 846-855."
    ),
    "source_doi": "10.1021/acs.oprd.4c00527",
    "source_url": "https://doi.org/10.1021/acs.oprd.4c00527",
    "license_url": "https://creativecommons.org/licenses/by/4.0/",
}
SAMPLES = [
    {
        "id": "example-job-scheme2",
        "title": "Example · Scheme A",
        "sub": "CC BY 4.0 Figure input · asymmetric synthesis",
        "source_type": "figure",
        "filename": "example-scheme-a.png",
        "src": REPO / "product_samples/example-scheme-a.png",
        "rights": EXAMPLE_SAMPLE_RIGHTS,
    },
    {
        "id": "example-job-scheme3",
        "title": "Example · Scheme B",
        "sub": "CC BY 4.0 Figure input · four-step synthesis",
        "source_type": "figure",
        "filename": "example-scheme-b.png",
        "src": REPO / "product_samples/example-scheme-b.png",
        "rights": EXAMPLE_SAMPLE_RIGHTS,
    },

]


_SAMPLE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$")
_SAMPLE_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}
_SAMPLE_FORBIDDEN_KEYS = {"gt", "ground_truth", "accepted", "reviewed", "score", "prediction", "result"}


def _external_product_samples() -> list[dict]:
    configured = os.getenv("CHEMRXN_PRODUCT_SAMPLE_ROOT", "").strip()
    if not configured:
        return []
    root = Path(configured).expanduser().resolve()
    manifest_path = root / "manifest.json"
    manifest = _read_json(manifest_path)
    rows = manifest.get("samples") if isinstance(manifest, dict) else None
    if not isinstance(rows, list):
        return []
    samples = []
    for raw in rows[:64]:
        if not isinstance(raw, dict):
            continue
        raw_keys = {str(key).lower() for key in raw}
        if any(token in key for key in raw_keys for token in _SAMPLE_FORBIDDEN_KEYS):
            continue
        sid = raw.get("id")
        title = raw.get("title")
        relative = raw.get("file")
        source_type = raw.get("sourceType", "figure")
        if not isinstance(sid, str) or not _SAMPLE_ID_RE.fullmatch(sid):
            continue
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            continue
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            continue
        if source_type != "figure":
            continue
        src = (root / relative).resolve()
        try:
            src.relative_to(root)
        except ValueError:
            continue
        expected_suffixes = _SAMPLE_IMAGE_SUFFIXES
        if src.suffix.lower() not in expected_suffixes or not src.is_file() or src.is_symlink():
            continue
        sub = raw.get("sub") if isinstance(raw.get("sub"), str) else "Source-only example"
        group = raw.get("group") if isinstance(raw.get("group"), str) else "Other"
        tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
        samples.append({
            "id": sid,
            "title": title.strip(),
            "sub": sub[:180],
            "group": group[:60],
            "tags": [str(tag)[:32] for tag in tags[:6] if isinstance(tag, str)],
            "source_type": source_type,
            "filename": raw.get("filename") if isinstance(raw.get("filename"), str) else src.name,
            "src": src,
        })
    return samples


def _product_samples() -> list[dict]:
    rows = [dict(sample, group="Bundled", tags=[]) for sample in SAMPLES]
    seen = {sample["id"] for sample in rows}
    for sample in _external_product_samples():
        if sample["id"] not in seen:
            rows.append(sample)
            seen.add(sample["id"])
    available = []
    for sample in rows:
        sources = _product_sample_sources(sample)
        if sources and all(Path(source["src"]).is_file() for source in sources):
            available.append(sample)
    return available


def _product_sample_sources(sample: dict) -> list[dict]:
    """Normalize legacy one-file samples and paired document samples.

    The private ``src`` paths remain server-only. Callers that serialize sample
    metadata must use :func:`_public_product_sample_files` instead.
    """
    sources = sample.get("sources")
    if isinstance(sources, list):
        if len(sources) != 2:
            return []
        normalized = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            role = source.get("role")
            filename = source.get("filename")
            src = source.get("src")
            if (
                role not in {"main", "si"}
                or not isinstance(filename, str)
                or not filename
                or Path(filename).name != filename
                or not src
            ):
                return []
            normalized.append({"role": role, "filename": filename, "src": Path(src)})
        if {source["role"] for source in normalized} != {"main", "si"}:
            return []
        return normalized
    if not isinstance(sample.get("filename"), str) or not sample.get("src"):
        return []
    role = "figure" if sample.get("source_type") == "figure" else "main"
    return [{"role": role, "filename": sample["filename"], "src": Path(sample["src"])}]


def _public_product_sample_rights(sample: dict) -> dict:
    """Expose only repository-reviewed rights claims; external manifests are untrusted."""
    claim = sample.get("rights")
    required = {
        "status": "verified",
        "license": "cc-by-4.0",
        "label": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
    }
    if not isinstance(claim, dict) or any(claim.get(key) != value for key, value in required.items()):
        return {"status": "undeclared", "label": "Local sample"}
    return {
        key: claim[key]
        for key in (
            "status", "license", "label", "attribution", "source_doi",
            "source_url", "license_url",
        )
        if isinstance(claim.get(key), str)
    }


def _public_product_sample_files(sample: dict) -> list[dict]:
    sid = urllib.parse.quote(sample["id"])
    sources = _product_sample_sources(sample)
    paired = len(sources) > 1
    return [
        {
            "role": source["role"],
            "filename": source["filename"],
            "url": (
                f"/api/samples/{sid}?{urllib.parse.urlencode({'source': source['role']})}"
                if paired else f"/api/samples/{sid}"
            ),
        }
        for source in sources
    ]


@app.get("/api/samples")
def api_samples():
    """List source-only examples; no GT, result, score, or local path is exposed."""
    result = []
    for sample in _product_samples():
        files = _public_product_sample_files(sample)
        row = {
            "id": sample["id"],
            "title": sample["title"],
            "sub": sample["sub"],
            "group": sample.get("group", "Other"),
            "tags": sample.get("tags", []),
            "sourceType": sample["source_type"],
            "rights": _public_product_sample_rights(sample),
            "files": files,
            # Preserve the original one-file listing contract while the product
            # upload island migrates to ``files`` for Main + SI.
            "filename": files[0]["filename"] if len(files) == 1 else None,
            "previewUrl": (
                f"/api/samples/{urllib.parse.quote(sample['id'])}"
                if sample["source_type"] == "figure" else None
            ),
        }
        result.append(row)
    return result


@app.get("/api/samples/{sid}")
def api_sample(sid: str, source: Optional[str] = None):
    sample = next((item for item in _product_samples() if item["id"] == sid), None)
    if not sample:
        raise HTTPException(404, "no such sample")
    sources = _product_sample_sources(sample)
    if source is None:
        # Direct retrieval of legacy samples remains byte-for-byte compatible.
        # For a paired sample, Main is a conservative compatibility default;
        # new clients should follow the exact role URLs returned by the listing.
        selected = next((item for item in sources if item["role"] == "main"), sources[0])
    else:
        selected = next((item for item in sources if item["role"] == source), None)
        if selected is None:
            raise HTTPException(404, "no such sample source")
    return FileResponse(
        str(selected["src"]), filename=selected["filename"],
        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


BENCH_DIR = REPO / "results" / "raw" / "pdf_bench"


@app.get("/api/benchmark")
def api_benchmark():
    """Cross-figure benchmark: Agent vs Baseline-Gemini vs MolParser across the pdf_benchmark figures."""
    rows = []
    if BENCH_DIR.exists():
        for f in sorted(BENCH_DIR.glob("*__*.json")):
            r = _read_json(f)
            if r and "paper" in r:
                rows.append(r)

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None

    summary = {"n_figures": len(rows)}
    for m in ("molparser", "baseline", "agent"):
        rs = [r[m] for r in rows if m in r]
        if not rs:
            continue
        s = {"smiles_f1": mean(x["smiles"]["f1"] for x in rs),
             "smiles_recall": mean(x["smiles"]["recall"] for x in rs),
             "smiles_precision": mean(x["smiles"]["precision"] for x in rs)}
        if m in ("agent", "baseline"):
            s["r"] = mean(x.get("r") for x in rs)
            s["avg_tokens_in"] = round(mean(x.get("tokens_in") or 0 for x in rs) or 0)
            s["avg_tokens_out"] = round(mean(x.get("tokens_out") or 0 for x in rs) or 0)
            s["total_elapsed_s"] = round(sum((x.get("elapsed_s") or 0) for x in rs))
        summary[m] = s
    return {"summary": summary, "rows": rows}


@app.get("/api/benchmark/figure")
def api_benchmark_figure(paper: str, fig: str):
    """Per-figure molecule detail: each method's correct / missed / extra SMILES vs GT (canonical)."""
    from tools_product.score_paper import norm_mol
    d = REPO / "pdf_benchmark" / paper
    gtf = next((d / f"{fig}{s}.json" for s in ("_output.reviewed", "_predicted_output.reviewed")
                if (d / f"{fig}{s}.json").exists()), None)
    if not gtf:
        raise HTTPException(404, "no GT")
    gt_set = _v2_mols(_read_json(gtf) or {})
    dets = _read_json(d / f"{fig}_molparser.json") or []
    mp = {c for c in (norm_mol(x.get("smi") or "", False) for x in dets if isinstance(x, dict)) if c and "*" not in c}

    def pred_set(method):
        for p in (BENCH_DIR / "preds" / f"{paper}__{fig}__{method}.json",
                  Path(f"/tmp/bench_{method}_{paper}_{fig}.json")):   # /tmp fallback for the in-flight run
            v = _read_json(p)
            if v is not None:
                return _v2_mols(v)
        return None

    out = {"paper": paper, "fig": fig, "gt_n": len(gt_set)}
    for name, s in (("molparser", mp), ("baseline", pred_set("baseline")), ("agent", pred_set("agent"))):
        if s is None:
            continue
        out[name] = {"correct": sorted(s & gt_set), "missed": sorted(gt_set - s)[:60],
                     "extra": sorted(s - gt_set)[:60], **_cmp(s, gt_set)}
    return out


@app.get("/benchmark", response_class=HTMLResponse)
def benchmark_page():
    f = REPO / "service" / "benchmark.html"
    html = f.read_text() if f.exists() else "<h2>benchmark.html missing</h2>"
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/version")
def api_version():
    """Which deployment + build the user is looking at (local / server-stable / server-dev). Drives
    the UI badge, and carries the reviewer base so the frontend never hardcodes port 8767."""
    return {**ENV_INFO, "reviewer_base": REVIEWER_BASE}


@app.get("/healthz")
def healthz():
    _, document_mode = _ui_bundle()
    return {"ok": bool(document_mode), "jobs": len(JOBS), "reviewer": REVIEWER_BASE,
            "version": ENV_INFO, "pdf_root": str(PDF_ROOT),
            "ui": {"document_mode": document_mode},
            "caps": {"iters": CAP_ITERS, "tokens": CAP_TOKENS},
            "price_per_m": {"in": PRICE_IN, "out": PRICE_OUT}}


# ─────────────── minimal fallback UI (Phase 2 replaces with harness_index.html) ───────────────
_FALLBACK_HTML = """<!doctype html><meta charset=utf-8><title>ChemReaction extractor</title>
<style>body{font:15px system-ui;margin:40px;max-width:760px}#drop{border:2px dashed #888;border-radius:10px;
padding:40px;text-align:center;color:#555}#log{white-space:pre-wrap;background:#111;color:#0f0;padding:12px;
border-radius:8px;margin-top:16px;min-height:60px}a{color:#06c}</style>
<h2>Upload a reaction figure &rarr; skill agent extracts it</h2>
<div id=drop>Drop a PNG/JPG here, or <input type=file id=f accept=image/*></div>
<div id=log>idle.</div>
<script>
const log=document.getElementById('log');
function show(t){log.textContent=t;}
async function go(file){
  show('uploading '+file.name+' ...');
  const fd=new FormData(); fd.append('file',file);
  const r=await fetch('/api/extract',{method:'POST',body:fd}); const {job_id}=await r.json();
  poll(job_id);
}
async function poll(id){
  const r=await fetch('/api/jobs/'+id); const j=await r.json();
  let t='job '+id+'\\nstatus: '+j.status+'  phase: '+j.phase+'  ('+(j.elapsed_s||0)+'s)';
  if(j.n_detections!=null)t+='\\ndetections: '+j.n_detections;
  if(j.error)t+='\\nERROR: '+j.error;
  if(j.status==='done'||j.status==='incomplete'){
    t+='\\nexamples: '+(j.n_examples||0);
    if(j.reason)t+='  reason: '+j.reason;
    if(j.reviewer_url)t+='\\n\\nOPEN IN REVIEWER: '+j.reviewer_url;
    show(t);
    if(j.reviewer_url){const a=document.createElement('a');a.href=j.reviewer_url;a.target='_blank';
      a.textContent='\\u2192 open extraction in chem_reviewer';a.style.display='block';a.style.marginTop='10px';log.after(a);}
    return;
  }
  if(j.status==='error'){show(t);return;}
  show(t); setTimeout(()=>poll(id),1500);
}
const f=document.getElementById('f'), drop=document.getElementById('drop');
f.onchange=e=>e.target.files[0]&&go(e.target.files[0]);
drop.ondragover=e=>{e.preventDefault();drop.style.background='#eef';};
drop.ondragleave=()=>drop.style.background='';
drop.ondrop=e=>{e.preventDefault();drop.style.background='';e.dataTransfer.files[0]&&go(e.dataTransfer.files[0]);};
</script>"""


if __name__ == "__main__":
    import uvicorn
    _ensure_ketcher()
    _mark_orphans_on_boot()
    # Load the deterministic PDF E-SMILES validator before the server reports
    # ready. On a fresh macOS chemistry environment the first RDKit-backed
    # import can exceed the browser's review-preview timeout; paying that
    # bounded cost at startup keeps Review changes -> Apply responsive.
    try:
        _validate_pdf_esmiles_structure("C<sep>")
    except Exception as exc:
        print(
            "[harness_server] PDF E-SMILES validator preflight unavailable: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    print(f"[harness_server] http://localhost:{PORT}   reviewer: {REVIEWER_BASE}   PDF_ROOT: {PDF_ROOT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
