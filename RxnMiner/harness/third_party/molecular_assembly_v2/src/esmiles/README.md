# eSMILES

可复用包：`body<sep>tags` 协议 → 规范化 / 展开缩写 → RDKit Mol → 写回。  
业务层请直接：

```python
from esmiles import (
    load_esmiles,
    normalize_target_input,
    normalize_fragment_esmiles,
    load_target_mol,
    expand_template_abbreviations,
    encode_substituent_esmiles,
)
```

根上旧模块名（`esmiles_loader` / `esmiles_ingress` / `abbrev_expand` 等）**已删除**，不要再 import。

---

## 分层

| 子模块 | 职责 |
|--------|------|
| `esmiles.loader` | DP 对齐解析 → Mol + `atom_by_position` + 锚点 |
| `esmiles.ingress` | 产物分类 `concrete` / `fragment`，规范化入口 |
| `esmiles.abbrev` | 缩写嫁接：**走 `chemistry.graft` → assembly 拼接核**（与 R 配对同一套）；**`CN` = 腈 `C#N`，不用异腈**；入口里先跑 `CH2?k` |
| `esmiles.methylene` | 亚甲基重复：`CH2?k` 薄适配插链；目标驱动解析 degree-2 `CH2?n` / 范围 bridge；display `(CH2)…` |
| `esmiles.encode` | Mol / dummy 写回 `<sep><a>pos:label</a>` |
| `esmiles.contract` | 连接点 / 虚拟弧契约（peel / align） |

## 推荐调用顺序

```text
字符串进来
  → normalize_fragment_esmiles / normalize_target_input   # 先展开 + 分类
  → load_esmiles / load_target_mol                         # 再进 Mol
  → 化学逻辑 (peel / assemble / match)
  → encode_substituent_esmiles                             # 需要写回时
```

业务层不要直接 `MolFromSmiles(body)` 再手抠 `<a>`。

用户可见画图走 `rendering.render_structure`（内部应只调本包公开 API）。

---

## 入口：concrete vs fragment

Synfacts 里同一条 `E-SMILES` 语义不同，**不能假设实施例产物永远是完整具体分子**：

| 类型 | 典型形态 | 例子 |
|------|----------|------|
| **concrete** | 纯 SMILES，或展开后主体无 `*` | `c1ccc(C)cc1`、`N(...PMP展开后...)…` |
| **fragment** | 主体仍有 `*`，`<sep>` 标 R 位 | `*C#CC(/C=C/*)C<sep><a>0:R</a><a>6:Ar</a>` |
| **concrete + 缩写后缀** | 主体有 `*`，后缀是基团缩写 | `N(...)*<sep><a>26:PMP</a>` → 展开进主体 |

历史上一律剥 `<sep>` 只适合 concrete；对 fragment 会丢连接位标签。

### 设计原则

1. **缩写尽早展开**（`expand_template_abbreviations` / ingress 内）——**abbrev graft** 必须走 `chemistry.graft`，与 `assemble_fragments` 同一接枝核；**禁止**在 `esmiles/` 再写一套 abbrev 用的 `RWMol` merge。`esmiles.methylene` 的位点插链是刻意例外（薄适配，不走双端口 assemble）。
2. **按类型分支**，不一刀切剥后缀。
3. **fragment 保留整行**；**concrete 只留 SMILES 主体**。
4. **幂等兜底**：`reverse_assemble` 内可再次 normalize。
5. **立体判据**：`ChiralTag` / `@` parity；CIP 与楔线不当接枝正确性标准（见 `.cursor/rules/graft-single-kernel.mdc`）。CH2 亚甲基扩链**不**做手性适配（见下节边界）。

### 核心 API

| 函数 | 作用 |
|------|------|
| `normalize_fragment_esmiles(line)` | 模板/fragment：展开缩写，保留 `<sep>` |
| `normalize_target_input(smiles)` | 产物侧：展开 → 分类 → `NormalizedTarget` |
| `classify_target_esmiles(line)` | `concrete` / `fragment`（展开后） |
| `load_target_mol(normalized)` | concrete → `MolFromSmiles`；fragment → `load_esmiles` |

### 分类规则（展开后）

1. 无 `<sep>` → **concrete**
2. 仅空 `<sep>` → **concrete**（剥后缀）
3. 主体无 `*` → **concrete**
4. 主体仍有 `*` 且后缀含开放槽位（`R` / `Ar` / 非已知缩写如 `NHQ`）→ **fragment**
5. 其余 → **concrete**

### 管线落点

| 输入 | 规范化 | 加载 |
|------|--------|------|
| 实施例产物 | `normalize_target_input` | `load_target_mol` |
| 产物 Markush 模板 | `normalize_fragment_esmiles` | `load_esmiles` |
| 反应物 Markush 模板 | `expand_reactant_template_lines` | `assemble_fragments` |

`assembly.normalize_target_smiles` 是对本包的薄封装。

---

## `loader` 在做什么

**只做**「字符串协议 → 可计算分子图」，**不做**业务分类、缩写展开、画图。

```text
"*C[C@@H](*)...<sep><a>0:CN</a><a>3:R</a>"
        │
        ▼  load_esmiles()
  split body / extension
  MolFromSmiles(body) 或 Indigo
  build_atom_by_position   ← <a> 数字 = SMILES 位序，不是随意 RDKit idx
  解析 <a>/<c>/<d>/<v>/<g>
  可选删 <d> dummy
  给 * 打 _R_label / 语义同位素
        │
        ▼
  EsmilesLoadResult(mol, atom_by_position, anchor_slots, ...)
```

对 `CH2?…`：loader **能**把 `*` 载入 RDKit Mol（`display_label` 已规范为 `(CH2)…`）；**拓扑展开**在下一步 `expand_template_abbreviations` / `normalize_fragment_esmiles`，不是 loader 职责。

---

## 亚甲基重复 `CH2?…`

位点上的 `(CH₂)ₖ` / `(CH₂)ₙ`（**不是** `|Sg:n|` / `<g>` 整段聚合物）。

| wire | `load_esmiles` | `display_label` | `expand_template_abbreviations` |
|------|----------------|-----------------|----------------------------------|
| `…*<sep><a>{pos}:CH2?3</a>` | 得到带 `*` 的 Mol | `(CH2)3` | 薄适配插成 k 个亚甲基 |
| `…*…<sep><a>{pos}:CH2?n</a>` | degree 2 时为 bridge spec | `(CH2)n` | 先保留；给定真实 target 后可推断 `n` 并具体化 |

```text
eSMILES（CH2?k / CH2?n）
  ├─ load_esmiles          → Mol（* 仍在）+ display (CH2)…
  └─ expand_template_abbreviations
        1) expand_numeric_ch2_in_esmiles   # 仅数字 k
        2) substitute_known_abbrev_slots   # 表内缩写 graft
        3) （无抽象 CH2 时）Translator.refactor 兜底
```

要点：

- **不是** `abbrevs.csv` 项；与 graft **同流水线**（先 CH2 再 graft），避免「graft 成功就跳过 CH2」。
- 实现：[`esmiles/methylene.py`](methylene.py)；单测：`tests/test_methylene_ch2.py`；演示与边界：`notebooks/01-1_ch2_methylene_expand_dev.ipynb`；对照笔记：`src/translator/note_methylene_chain.md`。

**边界（刻意不做）**

- 认 **`CH2?…`**（数字 / `n` / 范围），并兼容真实语料中的历史写法
  **`CH[2]?…`**；不认 `CHCl?n` 等。
- 高分子 `|Sg|` / `<g>` / 通用双齿单体：缩写库无配套「带两 `*`」的高分子单体；本路径也不靠双端口 assemble 吃任意 linker。
- **不接手性**：有机场景里化学家几乎不会把带手性的 C 写进 `(CH2)n`（会歧义）；不为 CH2 做立体 parity。

### 抽象 bridge 的目标驱动解析

```python
from esmiles import resolve_abstract_methylene_bridges

resolved = resolve_abstract_methylene_bridges(
    "C1C*CC1<sep><a>2:CH2?n</a>",
    "C1CCCCC1",
)
assert resolved[0].assignments[0].count == 2
```

解析只支持 degree-2 bridge。候选长度会先走现有 `CH2?k` 具体化，再以完整模板
嵌入真实 target；新增路径原子必须是中性、非芳香、全单键、无额外重原子支链
的真正 CH2。链可以参与成环。degree-1 terminal 因缺少第二固定端点而不猜测。
需要继续跑 Step0 时调用 `chemistry.step0_scaffold_map_resolving_repeats`。

---

## 格式规范（与 DP 对齐）

### 整体结构

```text
{SMILES}<sep>{extension}
```

- 分隔符：`ESMILES_SEPARATOR = "<sep>"`
- 无 `<sep>` 时视为纯 SMILES（仍可 `load_esmiles`）

### 标签类型

| 标签 | 语法 | Python 行为 |
|------|------|-------------|
| R 连接点 | `<a>{pos}:{label}</a>` | `RTagSlot`；`pos` → `atom_by_position` |
| Markush 环 | `<c>{pos}:{tag}?</c>` | 元数据 `_esmiles_c_*` |
| 哑原子 | `<d>{pos}:<dum></d>` | 删除该 `*` |
| 虚拟弧 | `<v>{pos}:…</v>` | 元数据 + contract |
| S-group | `<g>…</g>` / `\|Sg:…\|` | `EsmilesSGroupTag` |
| 子结构 | `<r>…</r>` | 解析 `<a>/…` 时跳过嵌套块 |

`<a>` 里的数字是 **SMILES 原子输出序**（与 Indigo `atom_order` 一致），不是任意 RDKit 索引。`build_atom_by_position` 优先读 `_smilesAtomOutputOrder`。

### R 槽位命名（`r_groups.py`）

- 槽位名来自 `<a>pos:label</a>` 的 label；重复时加 `#2`、`#3`
- `display_label`：图上短名（含 `CH2?3` → `(CH2)3`）；`pair_key`：组装配对键

---

## 校验

```python
from esmiles import validate_esmiles_line
issues = validate_esmiles_line(line)  # [] 表示 <a> 均指向 *
```

```bash
python tools/validate_esmiles_json.py --json-dir data/json
```

## 测试

```bash
conda activate datawork
python tests/test_esmiles_loader.py
python tests/test_esmiles_ingress.py
python tests/test_esmiles_contract.py
python tests/test_abbrev_expand.py
python tests/test_methylene_ch2.py
```

## 参考

- Indigo：`esmiles_loader.cpp`、`rdkit_esmiles.cpp`
- 变更：2026-06 引入 ingress（产物 fragment）；2026-07 收进本包并删除根上 shim；2026-07 合入 `CH2?k` / `CH2?n`（`esmiles.methylene`）
