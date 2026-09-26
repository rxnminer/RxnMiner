#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PKG_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
REPO_ROOT=$(cd "$PKG_ROOT/.." && pwd)
SKILL="$PKG_ROOT/skills/reaction-extract/SKILL.md"
EXT="$PKG_ROOT/pi-extension/index.ts"

usage() {
  cat <<'EOF'
Usage: figure_skills/scripts/run_pi_extract.sh IMAGE_OR_DIR [options]

Options:
  --out DIR             output root (default: ./chemrxn-runs/<UTC timestamp>)
  --cache FILE          immutable MolParser JSON for a single image
  --offline             require an immutable cache; never call live MolParser
  --allow-live          permit one live MolParser call on cache miss (default: auto)
  --concurrency 1|2     independent Pi sessions in parallel (default: 1)
  --provider NAME       Pi provider (default: openai-codex)
  --model NAME          Pi model (default: gpt-5.6-sol)
  --thinking LEVEL      off|minimal|low|medium|high|xhigh (default: medium)
  --dry-run             stage inputs and print commands without invoking Pi
  --no-reviewer         do not import sealed results into the Chem Reviewer
  -h, --help            show this help

For a directory, sibling caches should be named <image-stem>.molparser.json.
In auto mode a cache hit runs offline; a cache miss permits one explicit live call.
EOF
}

[[ $# -ge 1 ]] || { usage >&2; exit 2; }
if [[ "$1" == -h || "$1" == --help ]]; then usage; exit 0; fi
INPUT=$1
shift
OUT=""
CACHE=""
PERCEPTION=auto
CONCURRENCY=1
PROVIDER=${CHEMRXN_PI_PROVIDER:-openai-codex}
MODEL=${CHEMRXN_PI_MODEL:-gpt-5.6-sol}
THINKING=${CHEMRXN_PI_THINKING:-medium}
DRY_RUN=0
REGISTER_REVIEWER=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT=${2:?missing --out value}; shift 2 ;;
    --cache) CACHE=${2:?missing --cache value}; shift 2 ;;
    --offline) PERCEPTION=offline; shift ;;
    --allow-live) PERCEPTION=live; shift ;;
    --concurrency) CONCURRENCY=${2:?missing --concurrency value}; shift 2 ;;
    --provider) PROVIDER=${2:?missing --provider value}; shift 2 ;;
    --model) MODEL=${2:?missing --model value}; shift 2 ;;
    --thinking) THINKING=${2:?missing --thinking value}; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-reviewer) REGISTER_REVIEWER=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$CONCURRENCY" =~ ^[12]$ ]] || { echo "--concurrency must be 1 or 2" >&2; exit 2; }
[[ -f "$SKILL" && -f "$EXT" ]] || { echo "incomplete checkout: missing runtime Skill/extension" >&2; exit 2; }
command -v pi >/dev/null || { echo "Pi is not installed. Install Pi and run its login flow first." >&2; exit 3; }
[[ -e "$INPUT" ]] || { echo "input does not exist: $INPUT" >&2; exit 2; }
if [[ -n "$CACHE" && ! -f "$CACHE" ]]; then echo "cache does not exist: $CACHE" >&2; exit 2; fi
if [[ -d "$INPUT" && -n "$CACHE" ]]; then echo "--cache is only valid for one image" >&2; exit 2; fi

if [[ -z "$OUT" ]]; then OUT="$PWD/chemrxn-runs/$(date -u +%Y%m%dT%H%M%SZ)"; fi
[[ ! -e "$OUT" ]] || { echo "refusing to overwrite output root: $OUT" >&2; exit 2; }
mkdir -p "$OUT/cases" "$OUT/sessions" "$OUT/logs"
OUT=$(cd "$OUT" && pwd)

LIST="$OUT/input-list.txt"
if [[ -f "$INPUT" ]]; then
  printf '%s\n' "$(cd "$(dirname "$INPUT")" && pwd)/$(basename "$INPUT")" >"$LIST"
else
  find "$INPUT" -type f \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webp' -o -iname '*.tif' -o -iname '*.tiff' \) | sort >"$LIST"
fi
COUNT=$(wc -l <"$LIST" | tr -d ' ')
[[ "$COUNT" -gt 0 ]] || { echo "no supported images found" >&2; exit 2; }

PROMPT_FILE="$OUT/PROMPT.txt"
cat >"$PROMPT_FILE" <<'EOF'
Use the loaded reaction-extract Skill to extract every reaction from this ONE figure into strict V2 JSON.

Protocol:
- You are the sole chemistry decision-maker. Do not invoke another model, subagent, or workflow extractor.
- Read SKILL.md and LESSONS.md. Call detect exactly once. Inspect the full figure, overview, comparison grid, and every concrete audit crop. If detect fails, stop rather than retrying or substituting perception.
- Preserve accepted MolParser E-SMILES byte-verbatim. Every concrete bbox needs an image-grounded ocsr_audit; overrides need localized evidence and deterministic verification.
- Account for every arrow, scope/table row, independent reaction, failed example, intermediate, and product reuse in output/arrow_ledger.json.
- Use canonical R-group/FHR tools for every eligible undrawn reactant before model-authored fallback. Preserve inputs, results, closure, failure/partial status, and adoption decisions in output/tool_ledger.json.
- Verify all Agent-built/overridden structures and concrete tagged condition structures. Audit mapped stereo and explicit coordination/dative closures.
- Write output/plan.json, call assemble, inspect validation, write output/pred.v2.json, and render output/review.html. Do not claim success unless the final report says VERDICT: VALID.
- Write output/RUN_METADATA.json with model, repository pin, image/cache hashes, perception provenance, tool counts, validation status, timestamps, and success/partial/failure.
- Write output/SEAL.pre_reference.json containing SHA-256 for all final artifacts. Never search for GT, accepted/reviewed annotations, audit reports, sibling cases, or internet answers.
EOF

run_one() {
  set -o pipefail
  local image="$1"
  local stem base ext digest id case_root input_image source_cache offline log code
  base=$(basename "$image")
  stem=${base%.*}
  ext=${base##*.}
  digest=$(shasum -a 256 "$image" | awk '{print substr($1,1,12)}')
  id=$(printf '%s-%s' "$stem" "$digest" | tr -cs 'A-Za-z0-9._-' '_')
  case_root="$OUT/cases/$id"
  mkdir -p "$case_root/input" "$case_root/output" "$OUT/sessions/$id"
  input_image="$case_root/input/image.$ext"
  cp "$image" "$input_image"

  source_cache=""
  if [[ -n "$CACHE" ]]; then
    source_cache="$CACHE"
  elif [[ -f "${image%.*}.molparser.json" ]]; then
    source_cache="${image%.*}.molparser.json"
  elif [[ "$COUNT" == 1 && -f "$(dirname "$image")/molparser.json" ]]; then
    source_cache="$(dirname "$image")/molparser.json"
  fi
  if [[ -n "$source_cache" ]]; then cp "$source_cache" "$case_root/input/molparser.json"; fi

  case "$PERCEPTION" in
    offline)
      [[ -n "$source_cache" ]] || { echo "OFFLINE_CACHE_MISS $image" >&2; return 4; }
      offline=1 ;;
    live) offline=0 ;;
    auto) [[ -n "$source_cache" ]] && offline=1 || offline=0 ;;
  esac
  log="$OUT/logs/$id.json.gz"
  printf 'START %s %s cache=%s offline=%s\n' "$(date -u +%FT%TZ)" "$id" "${source_cache:-none}" "$offline"
  if [[ "$DRY_RUN" == 1 ]]; then
    printf 'DRY_RUN case=%s image=%s output=%s\n' "$id" "$input_image" "$case_root/output"
    return 0
  fi
  (
    cd "$case_root"
    export OCSR_OFFLINE="$offline" FHR_REVERSE=1
    export RXN_SKILLS_PYTHON=${RXN_SKILLS_PYTHON:-python3}
    pi --print --mode json \
      --name "figure-extract-$id" --session-dir "$OUT/sessions/$id" \
      --provider "$PROVIDER" --model "$MODEL" --thinking "$THINKING" \
      --no-context-files --no-skills --skill "$SKILL" \
      --no-extensions -e "$EXT" --approve \
      "@input/image.$ext" "$(cat "$PROMPT_FILE")"
  ) 2>&1 | gzip -1 >"$log" || code=$?
  code=${code:-0}
  if [[ "$code" != 0 ]]; then echo "FAIL process_exit=$code $id log=$log" >&2; return "$code"; fi
  if [[ ! -f "$case_root/output/SEAL.pre_reference.json" ]]; then
    echo "FAIL missing seal $id log=$log" >&2; return 5
  fi
  echo "DONE $(date -u +%FT%TZ) $id output=$case_root/output"
}
export OUT CACHE COUNT PERCEPTION PROVIDER MODEL THINKING DRY_RUN PROMPT_FILE SKILL EXT
export -f run_one

echo "Launching $COUNT image(s), concurrency=$CONCURRENCY, output=$OUT"
xargs -P "$CONCURRENCY" -I '{}' bash -c 'run_one "$1"' _ '{}' <"$LIST"
echo "COMPLETE output=$OUT"

if [[ "$DRY_RUN" == 0 && "$REGISTER_REVIEWER" == 1 ]]; then
  reviewer_failed=0
  while IFS= read -r case_root; do
    [[ -f "$case_root/output/pred.v2.json" ]] || continue
    review_args=(review "$case_root/output/pred.v2.json"
      --image "$case_root/input/$(basename "$(find "$case_root/input" -maxdepth 1 -type f -name 'image.*' | head -1)")"
      --collection figure-direct)
    if [[ -f "$case_root/output/detect/image_molparser.json" ]]; then
      review_args+=(--perception "$case_root/output/detect/image_molparser.json")
    fi
    if [[ "$COUNT" != 1 ]]; then review_args+=(--no-browser); fi
    echo "REGISTER reviewer case=$(basename "$case_root")"
    if ! "$REPO_ROOT/chemrxn" "${review_args[@]}"; then
      echo "WARN sealed extraction exists but Chem Reviewer import failed: $case_root" >&2
      reviewer_failed=1
    fi
  done < <(find "$OUT/cases" -mindepth 1 -maxdepth 1 -type d | sort)
  [[ "$reviewer_failed" == 0 ]] || exit 6
fi
