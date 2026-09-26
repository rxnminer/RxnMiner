#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
# A frozen checkout installed with `npm ci` contains Pi locally even when the participant has no
# global Pi binary. Keep participant credentials outside this shared code tree.
export PATH="$REPO/node_modules/.bin:$PATH"

usage() {
  cat <<'EOF'
Usage: run_distributed_shard.sh SHARD_DIR [--out DIR] [--concurrency 1|2]

Runs the frozen blind Figure extraction protocol with the participant's private Pi/Codex auth,
validates every returned case, and creates a checksummed tar.gz submission.
EOF
}

[[ $# -ge 1 ]] || { usage >&2; exit 2; }
[[ "$1" != -h && "$1" != --help ]] || { usage; exit 0; }
SHARD=$(cd "$1" && pwd)
shift
OUT="$SHARD/results"
CONCURRENCY=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT=${2:?missing --out}; shift 2 ;;
    --concurrency) CONCURRENCY=${2:?missing --concurrency}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ "$CONCURRENCY" =~ ^[12]$ ]] || { echo "--concurrency must be 1 or 2" >&2; exit 2; }
: "${PI_CODING_AGENT_DIR:?Set PI_CODING_AGENT_DIR to your private Pi auth/config directory}"
[[ -d "$PI_CODING_AGENT_DIR" ]] || { echo "private Pi directory does not exist: $PI_CODING_AGENT_DIR" >&2; exit 3; }

PIN=$(python3 - "$SHARD/SHARD_METADATA.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1])).get("repo_pin", ""))
PY
)
HEAD=$(git -C "$REPO" rev-parse HEAD)
[[ -n "$PIN" && "$HEAD" == "$PIN" ]] || {
  echo "frozen repository mismatch: required=$PIN actual=$HEAD" >&2
  exit 3
}
python3 "$HERE/validate_distributed_shard.py" --shard "$SHARD" --report "$SHARD/PREFLIGHT.json"
[[ ! -e "$OUT" ]] || { echo "refusing to overwrite result directory: $OUT" >&2; exit 2; }

"$REPO/figure_skills/scripts/run_pi_extract.sh" "$SHARD/input" \
  --out "$OUT" --offline --concurrency "$CONCURRENCY" \
  --provider openai-codex --model gpt-5.6-sol --thinking medium

python3 "$HERE/validate_distributed_shard.py" --shard "$SHARD" --result "$OUT" \
  --report "$OUT/VALIDATION.json"
cp "$SHARD/SHARD_METADATA.json" "$OUT/SHARD_METADATA.json"
cp "$SHARD/manifest.pre_reference.jsonl" "$OUT/manifest.pre_reference.jsonl"

ARCHIVE="$SHARD/$(basename "$SHARD").submission.tar.gz"
tar -C "$SHARD" -czf "$ARCHIVE" "$(basename "$OUT")"
shasum -a 256 "$ARCHIVE" >"$ARCHIVE.sha256"
echo "SUBMISSION_READY archive=$ARCHIVE checksum=$ARCHIVE.sha256"
