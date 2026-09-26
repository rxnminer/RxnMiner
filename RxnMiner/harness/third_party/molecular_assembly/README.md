# molecular_assembly

Synfacts 片段**组装**（正问题）与**骨架嵌入 / 扣 R**（逆问题）。依赖 **RDKit**（如 `conda activate datawork`）。

## 目录结构

```
molecular_assembly/
├── src/                      # 核心库（见 src/README.md）
│   ├── assembly.py           # 正问题：多片段组装
│   ├── decompose.py          # Step0 入口（薄 facade + 兼容 re-export）
│   ├── models.py             # dataclass
│   ├── anchors.py            # <a> 槽位解析
│   ├── align.py              # 嵌入枚举、键级
│   ├── peel.py               # 扣取代基、扣后骨架
│   ├── scaffold_geom.py      # 楔线/2D/手性帧
│   ├── stereo.py             # CIP 参考检查
│   ├── candidates.py         # 单候选构建与去重
│   ├── peel_topology.py      # 扣后 InChIKey
│   ├── r_groups.py
│   └── synfacts_json_cases.py
├── web/                      # Web 可视化（见 web/README.md）
│   ├── launch/               # 一键启动 API 服务
│   └── servers/              # FastAPI + 旧版 static HTML
├── frontend/                 # React 前端（Vite，见 frontend/README.md）
├── reference/                # 公司 Uni-Miner 前端参考（见 reference/README.md，不提交 clone）
├── batch/                    # 批量初筛
│   ├── run_decompose.py
│   └── logs/                 # 默认 batch 日志目录
├── json/                     # Synfacts 数据集
├── tests/
├── history/                  # 旧入口与运行残留（可删）
├── ma_paths.py               # 根路径 + setup_src_path()
├── run_api_server.py         # 统一 API → web/launch/api.py (:8765)
├── run_step0_server.py       # → web/launch/step0.py（旧版单服务）
├── run_batch.py
├── run_peel_viz.py           # → web/launch/peel.py
└── log.md
```

## 业务流水线（`src` 内）

```
模板骨架行 + 完整产物 SMILES          # scaffold_line + target_smiles
        │
        ▼
  step0_scaffold_map (decompose.py)   # 总入口：枚举嵌入 → 逐候选验证 → 去重汇总
        │
        ├─ align.collect_all_alignments
        │     # 在产物里找模板骨架的嵌入（B·子结构 / A·MCS键级 / A·MCS拓扑）
        │     # 输出多组 template_idx → full_idx 映射
        │
        ├─ candidates.build_candidate   # 对每一组映射建一条候选记录
        │     ├─ align.check_mapped_bond_orders
        │     │     # 键级检查：映射上的键是否与模板一致（单/双/芳香等）
        │     ├─ peel.peel_substituents
        │     │     # 扣 R：按连通块摘下取代基，得到扣后骨架 mol + 各 rN 的 SMILES
        │     ├─ peel_topology.check_peeled_topology_matches_template
        │     │     # 扣后结构✓：扣后骨架 vs 模板比 InChIKey（*→Ts + R 标签同位素）
        │     ├─ stereo.check_mapped_stereo_peeled
        │     │     # 扣后 CIP（参考）：映射位 R/S，取代基换 * 易与 InChIKey 结论不一致
        │     └─ stereo.check_mapped_stereo
        │           # 全分子 CIP（参考）
        │
        └─ candidates.dedupe_*
              # 去重：同 index 映射合并；同产物高亮区域只保留一条展示
```

**全通过**（与 UI / 批量一致）：`bond_order_ok` 且 `peel_topology_ok`（扣后 InChIKey / canonical，含立体层）。`peel_stereo_ok`、全分子 `stereo_ok` 仅参考。

## 常用命令

在 **`molecular_assembly/`** 下执行：

```bash
# Step0 主界面
python web/launch/run.py step0
# 或: python run_step0_server.py
# → http://<host>:8766

# 扣 R 手性逐步调试（开发用）
python web/launch/run.py peel
# 或: python run_peel_viz.py
# → http://<host>:8767

# 批量初筛（汇总 CSV：batch/logs/decompose_summary.csv）
python run_batch.py --json-dir json --limit 50
# 或: python batch/run_decompose.py ...

# 单测
python tests/test_peel_topology.py \\
  --template "*C…<sep><a>0:R[1]</a>…" --peel "*C(=O)…"
```

## 在代码里引用核心库

```python
from ma_paths import setup_src_path
setup_src_path()
from decompose import step0_scaffold_map
```

或把 `molecular_assembly/src` 加入 `PYTHONPATH`。

## 中间调试 / 可删除

| 可删 | 说明 |
|------|------|
| `history/` 整目录 | 旧 server、try 示例、artifacts |
| `web/servers/peel_stereo_viz.py` + `static/peel_stereo_viz.html` | 不用逐步手性排查时 |
| `tests/`、`run_peel_viz.py` | 开发单测 |
| `batch/logs/*.log` | 批量运行产物 |
| `log.md` | 个人笔记，非运行依赖 |

**勿删**：`src/`、`web/servers/decompose_server.py`、`web/servers/static/decompose.html`、`json/`（数据）。

## 环境

Python 3 · RDKit · FastAPI · uvicorn

## React 前端（前后端分离）

独立前端在 `frontend/`（Vite + React + TypeScript）。**推荐**只起一个统一 API：

```bash
python run_api_server.py       # :8765，含 decompose / reverse / balanced / abbrev
cd frontend && npm run dev     # :5173
```

旧版多端口启动仍可用，见 [web/README.md](web/README.md)。


成环的，检查不成环的部分，然后成环那明确检查是否连通
