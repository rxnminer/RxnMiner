# src — 核心库模块

| 模块 | 职责 |
|------|------|
| `assembly.py` | 正问题：片段组装、2D 画图 |
| `models.py` | `AnchorInfo`, `MatchCandidate`, `Step0MapResult` 等 |
| `anchors.py` | 解析 `<a>` 标签、`parse_anchors`、dummy↔R 标签映射 |
| `align.py` | 子结构/MCS 嵌入、`check_mapped_bond_orders` |
| `peel.py` | `peel_substituents`、切键、取代基 SMILES |
| `scaffold_geom.py` | 扣后楔线/虚线、2D 对齐、手性帧 `peel_parity_ok` |
| `stereo.py` | 全分子/扣后 CIP 对照（参考） |
| `candidates.py` | `build_candidate`、候选去重与排序 |
| `peel_topology.py` | 扣后 vs 模板 InChIKey（语义同位素） |
| `r_groups.py` | R 标签规范化、占位同位素数字 |
| `esmiles_loader.py` | **DP eSMILES 加载**（`<sep>` + 标签，见 `esmiles_README.md`） |
| `abbrev_expand.py` | Step0 前用 vendored `translator/` 展开模板缩写 |
| `translator/` | 自 `projects_on_git/Translator` 复制的缩写展开库 |
| `../tools/validate_esmiles_json.py` | 批量校验 JSON 中 E-SMILES `<a>` 标签 |
| `esmiles_encode.py` | 扣后取代基写回 `<sep><a>pos:label</a>` |
| `synfacts_json_cases.py` | 读 `../json/` 用例 |
| `batch_log.py` | 批量汇总 CSV（`jsonname,total,success`） |
| `batch_decompose.py` | 单 JSON 批量 Step0（CLI / 刷新 CSV） |
| `decompose.py` | **`step0_scaffold_map`** + 旧名 `_parse_anchors` 等 re-export |

依赖方向（简图）：

```
r_groups ──► anchors ──► align ──┐
                    └──► peel ──┼──► candidates ──► decompose (step0)
              scaffold_geom ◄──┘         ▲
              stereo ────────────────────┘
              peel_topology ─────────────┘
```
