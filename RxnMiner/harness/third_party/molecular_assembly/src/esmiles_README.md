# eSMILES 加载 — 功能报告

本模块 `src/esmiles_loader.py` 将 **DP / Indigo** 的 eSMILES 处理流程移植为 Python，供 `molecular_assembly`（Markush 拆分组装）使用，与 C++ 实现格式对齐。

## 背景

| 层级 | 实现 | 说明 |
|------|------|------|
| DP C++ | `esmiles_loader.cpp` + `rdkit_esmiles.cpp` | 官方 eSMILES 解析；SMILES 经 RDKit 进 Indigo `Molecule` |
| 旧 Python | `assembly.parse_fragment_line` + `MolFromSmiles` | 只拆 `<sep>`，忽略 `<c>/<d>/<v>` 等 |
| **新 Python** | `esmiles_loader.load_esmiles` | 按 DP 顺序解析标签；SMILES 参数对齐 `rdkit_esmiles` |

Synfacts 数据中的 **E-SMILES** 形如：

```text
*C(=O)[C@@H]1C[C@@]1(*)*<sep><a>0:Ar</a><a>6:R'</a><a>7:R</a>
```

`<sep>` 前为 SMILES 主体，后为扩展标签；标签里的 **数字是 SMILES 原子序号**（与 Indigo `atom_order` 一致），不是任意 RDKit 索引。

## 格式规范（与 DP 对齐）

### 整体结构

```text
{SMILES}<sep>{extension}
```

- 分隔符常量：`ESMILES_SEPARATOR = "<sep>"`
- 无 `<sep>` 时视为纯 SMILES（仍可通过 `load_esmiles` 加载主体）

### 标签类型

| 标签 | 语法 | C++ 对应 | Python 行为 |
|------|------|----------|-------------|
| R 连接点 | `<a>{pos}:{label}</a>` | `applyAtomTag` | 解析为 `RTagSlot`；`pos` 经 `atom_by_position` 映射到 RDKit 原子 |
| Markush 环 | `<c>{pos}:{tag}?</c>` | `applyCircTag` | 元数据写入 `_esmiles_c_*`（完整环展开仍依赖 Indigo） |
| 哑原子 | `<d>{pos}:<dum></d>` | `applyDummyTag` | 删除该位置 `*`，保留邻接 attachment |
| 虚拟弧 | `<v>{pos}:…</v>` | `applyVirtualArcTag` | 元数据 `_esmiles_v_*` |
| S-group | `<g>…</g>` / `\|Sg:…\|` | `applySGroupTag` | 解析为 `EsmilesSGroupTag` 列表 |
| 子结构 | `<r>…</r>` | `applyChildTag` | 解析 `<a>/<c>/…` 时 **跳过** `<r>` 块（与 C++ 一致） |

### 后缀

- `?` 后为 S-group 后缀（如 `CH[2]?n`），主值取 `?` 之前部分（`strip_sgroup_suffix`）

## 加载流程

```text
raw eSMILES
    │
    ├─ [可选] Indigo loadMolecule(raw)  → MolBlock → RDKit  （prefer_indigo=True 且已安装 epam.indigo）
    │
    └─ RDKit 路径（默认）
           split <sep>
           load SMILES（removeHs=False；含 ``->`` 时 sanitize 回退）
           build_atom_by_position
           parse <a>/<c>/<d>/<v>/<g> 标签
           apply <d> 哑原子删除
           resolve_anchor_dummy_indices  → anchor_slots
```

### SMILES 原子序号映射

`build_atom_by_position` 与 DP `rdkit_esmiles.cpp` 一致：优先读 `_smilesAtomOutputOrder`；否则用 `SmilesWriteParams(rootedAtAtom=0, canonical=False)` 生成序号表。`<a>pos` 必须通过此表映射到 RDKit 原子 idx。

### R 槽位命名（`r_groups.py`）

- 槽位名来自上游 `<a>pos:label</a>` 的 **label**（`Ar`、`R[1]`、`R'` …），不再生成内部 `r1`/`r2`。
- `internal_id` / `slot_key`：label；重复 label 时加 `#2`、`#3` 后缀以区分 peel 跟踪。
- `display_label`：图上短名（`R[4]'` → `R4'`）。
- `pair_key`：组装配对键（同源 label 可跨片段连接）。

### 校验

```python
from esmiles_loader import validate_esmiles_line
issues = validate_esmiles_line(line)  # [] 表示 <a> 位置均指向 *
```

批量扫描 Synfacts JSON：

```bash
python tools/validate_esmiles_json.py --json-dir json
```

### 与 Step0 集成

`decompose.step0_scaffold_map` 已改为：

```python
esmiles_loaded = load_esmiles(scaffold_line, apply_dummy=True, prefer_indigo=True)
anchors = anchors_from_slots(template_raw, esmiles_loaded.anchor_slots)
```

不再直接用 `MolFromSmiles(template_smiles)` + 手工 `<a>` 正则。

## 公开 API

| 函数 / 类型 | 用途 |
|-------------|------|
| `load_esmiles(raw)` | 完整加载，返回 `EsmilesLoadResult` |
| `load_esmiles_line(line)` | 同上别名 |
| `split_esmiles(raw)` | 仅拆分 body / extension |
| `parse_acdv_tags(info)` | 解析 `<a><c><d><v>` |
| `parse_sgroup_tags(info)` | 解析 S-group |
| `load_smiles_body(smiles)` | 只加载 `<sep>` 前 SMILES |
| `parse_fragment_line(line)` | 兼容旧接口 → `split_esmiles` |
| `anchors_from_slots(mol, slots)` | 在 `anchors.py`，由 slot 生成 `AnchorInfo` |

### `EsmilesLoadResult` 字段

- `mol` — RDKit 分子（供 peel / align 使用）
- `atom_by_position` — SMILES 序号 → 原子 idx
- `anchor_slots` — 已映射的 R 槽位
- `a_tags` / `c_tags` / `d_tags` / `v_tags` / `sgroup_tags`
- `backend` — `"rdkit"` 或 `"indigo"`
- `messages` — 非致命提示（如配位键回退）

## 能力边界

| 已支持 | 未完整移植（需 Indigo C++） |
|--------|---------------------------|
| `<sep>` + `<a>` 槽位与 Synfacts Step0 | `<r>` 子结构合并到主分子 |
| `<d>:<dum>` 哑原子删除 | `<v>` 虚拟弧几何与 KET 弧对象 |
| S-group 后缀 `?` 剥离 | 完整 S-group 写入分子图 |
| `<c>` Markush 环元数据 | Markush 环 S-group 实例化 |
| Indigo 可选后端（安装时） | Bingo / 反应 eSMILES |

安装 Indigo Python 后设置 `prefer_indigo=True`（默认）可尽量与 DP 线上一致；未安装时自动走 RDKit 路径。

## 测试

```bash
cd molecular_assembly
conda activate datawork   # 或你的 RDKit 环境
python tests/test_esmiles_loader.py
python tests/test_peel_topology.py --template "*C…<sep><a>0:R[1]</a>…" --peel "…"
```

## 参考源码（Indigo 仓库）

- `core/indigo-core/molecule/src/esmiles_loader.cpp` — 标签解析与 `loadEsmilesIntoMolecule`
- `core/indigo-core/molecule/src/rdkit_esmiles.cpp` — SMILES ↔ Indigo，`smilesToMoleculeWithInfo`
- `core/indigo-core/molecule/src/smiles_loader.cpp` — RDKit 失败时的 fallback

## 变更摘要

1. 新增 `src/esmiles_loader.py`
2. `decompose.py` — Step0 骨架行改走 `load_esmiles`
3. `anchors.py` — 抽出 `anchors_from_slots`；`template_dummy_slot_notes` 用 `display_label`
4. `assembly.py` — `build_complete_molecule` / `parse_fragment_line` 走 `load_esmiles`
5. `r_groups.py` — 槽位名来自 `<a>` label，非 `r1`/`r2`
6. `tools/validate_esmiles_json.py` — 批量校验 JSON 中 E-SMILES
7. `esmiles_encode.py` — 扣后取代基写回 ``<sep><a>pos:label</a>``（与连接 label 一致）
