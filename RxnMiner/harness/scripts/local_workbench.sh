#!/usr/bin/env bash
# local_workbench.sh — the supported LOCAL-FIRST launcher for the reaction image workspace + reviewer.
#
#   local_workbench.sh start  [--data-root DIR] [--port N] [--reviewer-port N] [--python BIN]
#                              [--runtime-port N] [--reviewer-dir DIR] [--no-reviewer] [--open]
#   local_workbench.sh status [--data-root DIR]
#   local_workbench.sh stop   [--data-root DIR]
#   local_workbench.sh urls   [--data-root DIR]        # print direct URLs for imported collections
#   local_workbench.sh clear-cache [--data-root DIR]   # wipe the rendered page cache
#
# Everything binds 127.0.0.1 only. Code lives in the repo; images/predictions/reviewer state live
# under the data root (default $CHEMRXN_VIEWER_DATA or ~/ChemRxn-viewer-data) and are never
# committed. No server paths, credentials or personal absolute paths belong in this file.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CMD="${1:-help}"; shift || true

DATA="${CHEMRXN_VIEWER_DATA:-$HOME/ChemRxn-viewer-data}"
VPORT=8787
RPORT=8767
PIPORT=4310
PYBIN="${CHEMRXN_VIEWER_PY:-}"
REVIEWER_DIR="${CHEMRXN_REVIEWER_DIR:-}"
LOCAL_OWNER="${CHEMRXN_LOCAL_RUNTIME_USER_ID:-local-user}"
NO_REVIEWER=0
OPEN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --data-root)     DATA="$2"; shift 2;;
    --port)          VPORT="$2"; shift 2;;
    --reviewer-port) RPORT="$2"; shift 2;;
    --runtime-port)  PIPORT="$2"; shift 2;;
    --python)        PYBIN="$2"; shift 2;;
    --reviewer-dir)  REVIEWER_DIR="$2"; shift 2;;
    --no-reviewer)   NO_REVIEWER=1; shift;;
    --open)          OPEN=1; shift;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
DATA="${DATA/#\~/$HOME}"
RUN="$DATA/run"; LOGS="$DATA/logs"; JOBS="$DATA/jobs"; RSTORE="$DATA/reviewer"

say()  { printf '%s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

find_python() {
  local cand
  for cand in "$PYBIN" "$DATA/python/bin/python" python3 python; do
    [ -n "$cand" ] || continue
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
      printf '%s' "$(command -v "$cand")"; return 0
    fi
  done
  return 1
}

check_deps() {  # verify the viewer's runtime deps up front, with actionable names
  local py="$1" missing=""
  local mod
  for mod in fitz fastapi uvicorn PIL; do
    "$py" -c "import $mod" 2>/dev/null || missing="$missing $mod"
  done
  if [ -n "$missing" ]; then
    fail "python $py is missing:$missing
  install:  $py -m pip install pymupdf fastapi uvicorn pillow
  or point --python / \$CHEMRXN_VIEWER_PY at a venv that has them"
  fi
  "$py" -c "import rdkit" 2>/dev/null || \
    say "note: rdkit not importable — molecule drawings degrade to labeled placeholders"
}

is_lfs_pointer() { head -c 60 "$1" 2>/dev/null | grep -q '^version https://git-lfs'; }

resolve_reviewer() {  # echoes the reviewer server.py path, or fails with the LFS explanation
  local dir="$REVIEWER_DIR"
  [ -n "$dir" ] || dir="$REPO/reviewer/chem_reviewer"
  local srv="$dir/server.py"
  [ -f "$srv" ] || fail "reviewer not found at $dir (set --reviewer-dir or \$CHEMRXN_REVIEWER_DIR)"
  if is_lfs_pointer "$srv"; then
    fail "reviewer source at $dir is still a Git-LFS POINTER, not a real file.
  Run git lfs pull for that legacy checkout, or pass --reviewer-dir /path/to/chem_reviewer."
  fi
  local ketcher="${KETCHER_DIR:-$dir/../vendor/ketcher}"
  [ -d "$ketcher" ] || ketcher="$dir/../ketcher"  # legacy external reviewer layout
  if [ ! -f "$ketcher/index.html" ] || is_lfs_pointer "$ketcher/index.html"; then
    fail "Ketcher assets are missing or still Git-LFS POINTERS at $ketcher.
  Either fetch them:      git -C '$REPO' lfs pull --include 'reviewer/vendor/ketcher/**,reviewer/chem_reviewer/web/lib/*.wasm'
  or use a real checkout: $0 start --reviewer-dir /path/to/chem_reviewer
  or skip the reviewer:   $0 start --no-reviewer"
  fi
  printf '%s' "$srv"
}

port_owner() { lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $1" (pid "$2")"}'; }

pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

pid_matches() {  # stale-PID recovery: the recorded pid must still run OUR command
  local pid="$1" needle="$2"
  ps -p "$pid" -o command= 2>/dev/null | grep -q "$needle"
}

read_pid() { cat "$RUN/$1.pid" 2>/dev/null || true; }

health_viewer() {  # identity check, not a bare 200: our /healthz reports pdf_root + version
  "$PY" - "$1" "$RSTORE" <<'EOF' 2>/dev/null
import json, sys, urllib.request
port, rstore = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as r:
    h = json.load(r)
ok = (h.get("ok") is True and isinstance(h.get("version"), dict) and
      h.get("ui", {}).get("document_mode") is True and "pdf_root" in h)
env = h["version"].get("env")
same_store = h.get("pdf_root", "").rstrip("/").endswith(rstore.rstrip("/").split("/")[-1]) or \
             h.get("pdf_root", "").rstrip("/") == rstore.rstrip("/")
sys.exit(0 if (ok and same_store) else (3 if ok else 4))
EOF
}

health_reviewer() {
  curl -fsS "http://127.0.0.1:$1/api/papers" >/dev/null 2>&1 || \
  curl -fsS "http://127.0.0.1:$1/static/" >/dev/null 2>&1
}

health_runtime() {
  curl -fsS "http://127.0.0.1:$1/healthz" 2>/dev/null | \
    grep -q '"extractionBackend":"figure_live_ready"'
}

spawn_daemon() {
  # `nohup ... &` still shares the npm/CLI process group on some platforms, so
  # npm can reap a newly started long-running image job when `chemrxn extract`
  # exits. Python's start_new_session performs the required POSIX double
  # boundary while preserving an exact child PID for status/stop.
  local cwd="$1" pidfile="$2" logfile="$3"
  shift 3
  "$PY" - "$cwd" "$pidfile" "$logfile" "$@" <<'PY'
import pathlib, subprocess, sys
cwd, pidfile, logfile, *command = sys.argv[1:]
pathlib.Path(logfile).parent.mkdir(parents=True, exist_ok=True)
stream = open(logfile, "ab", buffering=0)
proc = subprocess.Popen(
    command,
    cwd=cwd,
    stdin=subprocess.DEVNULL,
    stdout=stream,
    stderr=subprocess.STDOUT,
    close_fds=True,
    start_new_session=True,
)
pathlib.Path(pidfile).write_text(str(proc.pid))
PY
}

do_start() {
  PY="$(find_python)" || fail "no python >= 3.9 found; pass --python /path/to/venv/bin/python"
  check_deps "$PY"
  mkdir -p "$RUN" "$LOGS" "$JOBS" "$RSTORE"
  command -v node >/dev/null 2>&1 || fail "node >=22.19 is required for the local Pi runtime"
  node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 22 || (major === 22 && minor >= 19) ? 0 : 1)' || \
    fail "node >=22.19 is required for the local Pi runtime"
  if ! (cd "$REPO/runtime/pi-service" && node --input-type=module -e \
    'await import("@earendil-works/pi-coding-agent")' >/dev/null 2>&1); then
    fail "Pi runtime dependencies are missing. Install the package with npm, or run npm ci in the repository root."
  fi
  # A checkout may retain dist/ from an older commit. Validate the compiled
  # Skill pins against this checkout before launch. Rebuild only when missing or
  # stale: installable packages intentionally ship validated dist/ without the
  # TypeScript build toolchain.
  runtime_skills_valid() {
    (cd "$REPO/runtime/pi-service" && node --input-type=module -e \
      'const m=await import("./dist/src/skills.js"); await m.loadPinnedSkillManifest(process.argv[1]);' \
      "$REPO" >/dev/null 2>&1)
  }
  if ! runtime_skills_valid; then
    (cd "$REPO/runtime/pi-service" && npm run build >/dev/null) || \
      fail "Pi runtime is stale and rebuild failed. Run npm ci in the repository root and retry."
    runtime_skills_valid || fail "Rebuilt Pi runtime does not match the pinned repository Skills."
  fi
  # Complete reviewer preflight before starting any process. Otherwise a bad
  # checkout (for example an unresolved LFS pointer) can leave an orphaned Pi
  # runtime behind even though the launcher reports failure.
  local rsrv=""
  if [ "$NO_REVIEWER" != 1 ]; then
    rsrv="$(resolve_reviewer)"
  fi
  if [ ! -s "$RUN/runtime.token" ]; then
    umask 077
    "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))' >"$RUN/runtime.token"
  fi
  chmod 600 "$RUN/runtime.token"
  local runtime_token; runtime_token="$(cat "$RUN/runtime.token")"

  local name port
  for name in runtime viewer reviewer; do
    [ "$name" = reviewer ] && [ "$NO_REVIEWER" = 1 ] && continue
    if [ "$name" = runtime ]; then port="$PIPORT"
    elif [ "$name" = viewer ]; then port="$VPORT"
    else port="$RPORT"; fi
    local pid; pid="$(read_pid "$name")"
    local needle
    if [ "$name" = runtime ]; then needle="dist/src/server.js"
    elif [ "$name" = viewer ]; then needle="harness_server"
    else needle="server.py"; fi
    if pid_alive "$pid" && pid_matches "$pid" "$needle"; then
      fail "$name already running (pid $pid, port $port). Use '$0 status' or '$0 stop' first."
    fi
    [ -n "$pid" ] && { say "cleaning stale $name pidfile (pid $pid is gone)"; rm -f "$RUN/$name.pid"; }
    if owner="$(port_owner "$port")" && [ -n "$owner" ]; then
      fail "port $port is taken by $owner — stop it, or pass --port/--reviewer-port for another port"
    fi
  done

  spawn_daemon "$REPO/runtime/pi-service" "$RUN/runtime.pid" "$LOGS/runtime.log" \
    env CHEMRXN_PI_PORT="$PIPORT" CHEMRXN_PI_INTERNAL_TOKEN="$runtime_token" \
    CHEMRXN_PI_DATA_DIR="$DATA/runtime" CHEMRXN_LOCAL_AGENT_ENABLED=1 \
    CHEMRXN_REPO_ROOT="$REPO" CHEMRXN_LOCAL_WORKSPACE_ROOT="$DATA/agent-workspaces" \
    CHEMRXN_PI_AGENT_ROOT="$DATA/pi-agent" CHEMRXN_CHEM_PYTHON="$PY" \
    CHEMRXN_PI_AUTH_PATH="${CHEMRXN_PI_AUTH_PATH:-$DATA/pi-auth/auth.json}" \
    CHEMRXN_PI_MODELS_PATH="${CHEMRXN_PI_MODELS_PATH:-$DATA/pi-auth/models.json}" \
    PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 \
    node dist/src/server.js

  if [ "$NO_REVIEWER" != 1 ]; then
    spawn_daemon "$(dirname "$rsrv")" "$RUN/reviewer.pid" "$LOGS/reviewer.log" \
      env PDF_ROOT="$RSTORE" \
      CHEMRXN_REVIEWER_TRUSTED_ORIGINS="http://127.0.0.1:$RPORT,http://localhost:$RPORT,http://127.0.0.1:$VPORT,http://localhost:$VPORT" \
      "$PY" "$rsrv" --host 127.0.0.1 --port "$RPORT" --data-root "$RSTORE"
  fi

  spawn_daemon "$REPO/harness" "$RUN/viewer.pid" "$LOGS/viewer.log" \
    env JOBS_DIR="$JOBS" PORT="$VPORT" REVIEWER_BASE="http://127.0.0.1:$RPORT" \
    PDF_ROOT="$RSTORE" CR_ENV=local CHEMRXN_PI_RUNTIME_BASE="http://127.0.0.1:$PIPORT" \
    CHEMRXN_PI_INTERNAL_TOKEN="$runtime_token" CHEMRXN_LOCAL_RUNTIME_USER_ID="$LOCAL_OWNER" \
    CHEMRXN_VIEWER_DATA="$DATA" \
    CHEMRXN_PRODUCT_SAMPLE_ROOT="${CHEMRXN_PRODUCT_SAMPLE_ROOT:-$DATA/product-samples}" \
    CHEMRXN_DISTRIBUTED_RUNS_DIR="${CHEMRXN_DISTRIBUTED_RUNS_DIR:-$DATA/distributed-runs}" \
    CHEMRXN_DISTRIBUTED_CODE_ROOT="${CHEMRXN_DISTRIBUTED_CODE_ROOT:-$REPO}" \
    CHEMRXN_DISTRIBUTED_PI_AGENT_DIR="${CHEMRXN_DISTRIBUTED_PI_AGENT_DIR:-$DATA/pi-auth}" \
    "$PY" service/harness_server.py

  cat >"$RUN/local_viewer.json" <<EOF
{"viewer_port": $VPORT, "reviewer_port": $RPORT, "runtime_port": $PIPORT, "data_root": "$DATA",
 "python": "$PY", "no_reviewer": $NO_REVIEWER, "started": $(date +%s)}
EOF

  local i ok=0
  # A cold Node/Pi SDK plus native chemistry preflight can take multiple
  # minutes on a fresh macOS venv. Keep the wait bounded, but longer than the
  # runtime's own 180-second Python gate.
  for i in $(seq 1 420); do
    if health_runtime "$PIPORT"; then ok=1; break; fi
    pid_alive "$(read_pid runtime)" || break
    sleep 0.5
  done
  if [ "$ok" != 1 ]; then
    say "--- runtime.log tail ---"; tail -n 20 "$LOGS/runtime.log" >&2 || true
    fail "runtime failed its health check on :$PIPORT (see $LOGS/runtime.log)"
  fi
  say "runtime ready: http://127.0.0.1:$PIPORT   (pid $(read_pid runtime), state: $DATA/runtime)"

  ok=0
  # The viewer preloads the deterministic PDF E-SMILES validator so the first
  # Review changes action does not inherit a cold RDKit import.
  for i in $(seq 1 360); do
    if health_viewer "$VPORT"; then ok=1; break; fi
    pid_alive "$(read_pid viewer)" || break
    sleep 0.5
  done
  if [ "$ok" != 1 ]; then
    say "--- viewer.log tail ---"; tail -n 20 "$LOGS/viewer.log" >&2 || true
    fail "viewer failed its identity health check on :$VPORT (see $LOGS/viewer.log)"
  fi
  say "viewer  ready: http://127.0.0.1:$VPORT   (pid $(read_pid viewer), jobs: $JOBS)"

  if [ "$NO_REVIEWER" != 1 ]; then
    ok=0
    for i in $(seq 1 360); do
      if health_reviewer "$RPORT"; then ok=1; break; fi
      pid_alive "$(read_pid reviewer)" || break
      sleep 0.5
    done
    if [ "$ok" != 1 ]; then
      say "--- reviewer.log tail ---"; tail -n 20 "$LOGS/reviewer.log" >&2 || true
      fail "reviewer failed on :$RPORT (see $LOGS/reviewer.log)"
    fi
    say "reviewer ready: http://127.0.0.1:$RPORT/static/   (pid $(read_pid reviewer), store: $RSTORE)"
  fi
  do_urls
  [ "$OPEN" = 1 ] && command -v open >/dev/null && open "http://127.0.0.1:$VPORT" || true
}

do_status() {
  PY="$(find_python || true)"
  local name pid needle port state
  for name in runtime viewer reviewer; do
    pid="$(read_pid "$name")"
    if [ "$name" = runtime ]; then needle="dist/src/server.js"; port="$PIPORT"
    elif [ "$name" = viewer ]; then needle="harness_server"; port="$VPORT"
    else needle="server.py"; port="$RPORT"; fi
    if pid_alive "$pid" && pid_matches "$pid" "$needle"; then
      state="running (pid $pid)"
      if [ "$name" = runtime ]; then
        health_runtime "$port" && state="$state, healthy" || state="$state, HEALTH CHECK FAILED"
      elif [ "$name" = viewer ] && [ -n "$PY" ]; then
        health_viewer "$port" && state="$state, healthy" || state="$state, HEALTH CHECK FAILED"
      fi
    elif [ -n "$pid" ]; then
      state="STALE pidfile (pid $pid not ours/not alive) — run stop to clean"
    else
      state="stopped"
    fi
    say "$name@:$port  $state"
  done
  if [ -d "$JOBS/_collections" ]; then
    say "collections:"
    local f
    for f in "$JOBS/_collections"/*.jsonl; do
      [ -e "$f" ] || continue
      say "  $(basename "$f" .jsonl): $(grep -c . "$f") card(s)"
    done
  fi
  if [ -d "$JOBS" ]; then
    local n; n=$(find "$JOBS" -path '*/pdf_pages/*.png' 2>/dev/null | wc -l | tr -d ' ')
    say "page cache: $n png(s) ($(du -sh "$JOBS" 2>/dev/null | cut -f1) total job store)"
  fi
}

do_stop() {
  local name pid
  for name in runtime viewer reviewer; do
    pid="$(read_pid "$name")"
    if [ -z "$pid" ]; then say "$name: no pidfile"; continue; fi
    if pid_alive "$pid"; then
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 10); do pid_alive "$pid" || break; sleep 0.3; done
      pid_alive "$pid" && { kill -9 "$pid" 2>/dev/null || true; say "$name: killed (pid $pid)"; } \
                       || say "$name: stopped (pid $pid)"
    else
      say "$name: stale pidfile cleaned (pid $pid was not running)"
    fi
    rm -f "$RUN/$name.pid"
  done
}

do_urls() {
  [ -d "$JOBS/_collections" ] || { say "no collections yet — run rxnminer review with --image"; return 0; }
  local f id
  for f in "$JOBS/_collections"/*.jsonl; do
    [ -e "$f" ] || continue
    say "collection $(basename "$f" .jsonl):"
    while IFS= read -r line; do
      id=$(printf '%s' "$line" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
      [ -n "$id" ] && say "  http://127.0.0.1:$VPORT/?job=$id"
    done < "$f"
  done
}

do_clear_cache() {
  [ -d "$JOBS" ] || { say "no job store at $JOBS"; return 0; }
  local n; n=$(find "$JOBS" -path '*/pdf_pages/*.png' 2>/dev/null | wc -l | tr -d ' ')
  find "$JOBS" -path '*/pdf_pages/*.png' -delete 2>/dev/null || true
  say "cleared $n cached page render(s) under $JOBS (pages re-render lazily on next view)"
}

case "$CMD" in
  start)       do_start;;
  status)      do_status;;
  stop)        do_stop;;
  urls)        do_urls;;
  clear-cache) do_clear_cache;;
  *) sed -n '2,16p' "$0"; exit 2;;
esac
