#!/usr/bin/env bash
# add_to_viewer.sh - 把一批抽取结果(<id>.v2.json + <id>.png)加进 chem_reviewer 可视化并起服务。
# 用法:
#   bash scripts/add_to_viewer.sh <OUT_DIR> <PDF_ROOT> <PORT> [IMAGES_DIR]
# 例:
#   bash scripts/add_to_viewer.sh output/2026-08-01_myrun ../reviewer-data/myrun 8774
# 参数:
#   OUT_DIR    = 抽取输出目录(里面有 <id>.v2.json;通常也有 <id>.png)
#   PDF_ROOT   = 外部 reviewer 数据根(新数据集用新目录,不写入源码树)
#   PORT       = 服务端口(每套数据用一个端口,如 8774)
#   IMAGES_DIR = 图片目录(省略则默认 = OUT_DIR,因为跑批时 png 就在里面)
set -e
OUT_DIR="$1"; PDF_ROOT="$2"; PORT="$3"; IMAGES_DIR="${4:-$1}"
[ -z "$PORT" ] && { echo "用法: bash scripts/add_to_viewer.sh <OUT_DIR> <PDF_ROOT> <PORT> [IMAGES_DIR]"; exit 1; }
HERE="$(cd "$(dirname "$0")/.." && pwd)"          # harness 根
PROJECT_ROOT="$(cd "$HERE/.." && pwd)"
REVIEWER_DIR="${CHEMRXN_REVIEWER_DIR:-$PROJECT_ROOT/reviewer/chem_reviewer}"
PY="${CHEMRXN_VIEWER_PY:-/root/miniconda3/envs/LangGraph/bin/python}"
ABS_PDF="$(cd "$HERE" && mkdir -p "$PDF_ROOT" && cd "$PDF_ROOT" && pwd)"

echo "[1/2] 导入 $OUT_DIR -> $PDF_ROOT ..."
"$PY" "$HERE/scripts/import_release_to_reviewer.py" \
  --out-dir "$OUT_DIR" --images "$IMAGES_DIR" --pdf-root "$PDF_ROOT"

echo "[2/2] 起 viewer 服务 :$PORT (PDF_ROOT=$ABS_PDF) ..."
SESS="relv_${PORT}"
tmux kill-session -t "$SESS" 2>/dev/null || true
tmux new-session -d -s "$SESS" \
  "cd '$REVIEWER_DIR' && PDF_ROOT='$ABS_PDF' NO_STOP=1 '$PY' server.py --port $PORT >/tmp/${SESS}.log 2>&1"
sleep 4
echo "----"
echo "OK. tmux=$SESS  端口=$PORT"
echo "本地隧道:  ssh -fN -o ExitOnForwardFailure=no -L $PORT:127.0.0.1:$PORT dp_server2"
echo "浏览器:    http://127.0.0.1:$PORT/"
