# chemistry

正装 / 逆装核 / Step0（peel·align·candidates）/ 几何校验 / 渲染。

```python
from chemistry import reverse_assemble, step0_scaffold_map, assemble_fragments, render_structure
# 或按子模块：
from chemistry.peel import peel_substituents
from chemistry.assembly import mol_to_png_base64  # 底层 PNG；用户可见图优先 rendering
```

eSMILES 协议在并列包 [`../esmiles/`](../esmiles/)。**不要**把协议解析或 Synfacts JSON 编排塞回本包。

---

## 模块职责

### 门面

| 模块 | 职责 |
|------|------|
| `__init__.py` | 惰性导出 `reverse_assemble` / `step0_scaffold_map` / `assemble_fragments` / `render_structure`（避免与 `esmiles→r_groups` 循环导入） |

### 逆问题主链（Step0）

| 模块 | 职责 |
|------|------|
| `decompose` | **`step0_scaffold_map`**：枚举嵌入 → 建候选 → 去重汇总 |
| `align` | 子结构 / MCS 嵌入、键级检查 |
| `peel` | 按嵌入扣取代基、切键、取代基 SMILES |
| `candidates` | 单候选组装、高亮图、排序去重 |
| `models` | `AnchorInfo` / `MatchCandidate` / `PeelResult` / `Step0MapResult` 等 |

### 正装 / 逆装核

| 模块 | 职责 |
|------|------|
| `assembly` | **正装**：R 标签配对、合分子；另含底层 `mol_to_png_*`（历史遗留，见债务） |
| `graft` | 缩写展开与正装共用的接枝入口（`*`←基团 → `build_complete_molecule`）。**唯一**允许的接枝实现；见 `.cursor/rules/graft-single-kernel.mdc`。 |
| `reverse_assembly` | **逆装核**：`reverse_assemble(target, scaffold, reactant_templates)` + 填反应物 / bridge 重标 / 与期望 SMILES 比对 |
| `assembly_export` | peel 结果 → 可正装的 eSMILES 行（roundtrip 桥） |

### 标签与占位

| 模块 | 职责 |
|------|------|
| `r_groups` | `<a>` label → `display_label` / `pair_key` / 语义同位素（`esmiles` 也依赖） |
| `anchors` | dummy↔R、从 eSMILES 抽锚点 |

### 校验 / 几何

| 模块 | 职责 |
|------|------|
| `peel_topology` | 扣后 vs 模板 InChIKey（语义同位素）、附件标签对称/重复检查 |
| `scaffold_geom` | 扣后楔线 / 2D / 手性帧 |
| `stereo` | CIP 对照（**仅参考**，不挡「全通过」） |
| `roundtrip` | 组装回产物校验 |
| `chem_util` | 分子等价比较等小工具 |

### 画图

| 模块 | 职责 |
|------|------|
| `rendering` | **用户可见**统一入口：`render_structure`（字符串→可选展开→PNG） |

### 环位置变化与抽象亚甲基 bridge

- `<r>ring_index:label</r>` 在 loader 中保持为未解析的环 selector；只有获得
  template→target alignment 后，才在所选环上枚举目标的非核心环外支链，并
  转成普通 `AnchorInfo(anchor_kind="ring")` 复用现有 peel/roundtrip。
- 环编号保留 RDKit `AtomRings()` 的构造顺序；不要再按最小 atom index 排序，
  否则会交换稠合体系中的苯环与含 N 五元环。
- 数字数量范围如 `<r>1:R?1-3</r>` 会枚举所选环上 1–3 个不复用的合法 site。
- 若所选环没有任何骨架外重原子/开放槽位，`<r>` 会在映射环的含氢位置
  解析为空取代：peel 记录 `*[H]`，并将隐式 H 传播到对应反应物模板；不会
  生成独立的 H2。只要存在可见环外支链，就优先使用支链而不枚举其它 H 位点。
- 逆组装会把产物侧已经解析的 `<r>` 环原子，通过保守环系 MCS 映射到反应物
  模板指定环，再实体化为普通 dummy 槽位复用组装核。映射不唯一或环索引无效
  时必须保留为未消费标签并判失败，不能把忽略 `<r>` 的结构报告为成功。
- `step0_scaffold_map_resolving_repeats` 先将 target 支持的 degree-2
  `CH2?n` bridge 具体化，再逐一调用普通 `step0_scaffold_map`。

Viewer（[`run_parser_results_viewer_dev.py`](../../run_parser_results_viewer_dev.py)）路线 B 应调：`reverse_assemble` + `attach_expected_comparisons` + `render_structure` / 现有出图路径；**不**依赖 Synfacts 编排模块。

---

## 不在本包（刻意拆出）

| 位置 | 内容 |
|------|------|
| [`web/synfacts/reverse_examples.py`](../../web/synfacts/reverse_examples.py) | 读官方 / `_no_batch` JSON：`template_steps_for_id`、`example_target_smiles`、`matching_official_example`、`reverse_assemble_from_example`、批报 `classify_failure_*`、缓存 `hydrate_*` |
| [`web/synfacts/json_cases.py`](../../web/synfacts/json_cases.py) / `image.py` | 语料 case 枚举与页图路径 |
| [`../esmiles/`](../esmiles/) | eSMILES load / ingress / abbrev / encode / contract |
| [`batch/`](../../batch/) | 批量循环与日志，不是算法 |

逆装核签名应保持：

```text
reverse_assemble(target, product_scaffold, reactant_templates, ...) -> ReverseAssemblyResult
```

JSON 实施例 → 抽 `target` / `scaffold` / `templates` 是 **web/batch** 的事。

---

## 诚实债务（别当已完成）

搬家进 `chemistry/` **不等于**职责已经干净。当前仍「掩耳盗铃」的点：

1. **`assembly` 仍扛底层 PNG**  
   `mol_to_png_*` / Markush 画图实现还在 `assembly.py`；`rendering` 只是外层入口，内部仍 `from chemistry.assembly import …`。  
   peel / reverse / roundtrip 也有不少直调 `mol_to_png_*`。

2. **`reverse_assembly` 仍含出图与「对期望 SMILES」比对**  
   `structure_png_base64`、`attach_expected_comparisons` 留在化学包，因为 parser viewer 直接用。它们不是 Synfacts schema，但是 **UI/评测旁路**，不是 peel→fill 的最小核。

3. **`ReverseAssemblyResult` 字段偏 UI**  
   大量 `*_image_base64`、yield、markush_enumeration、compare 字段绑在结果对象上，核与展示未分开。

4. **高亮子结构图未进 `rendering`**  
   「完整产物 (当前方案高亮)」仍在 `candidates` / peel 路径（需要映射原子/键）。

5. **`reverse_assembly` 文件仍偏大（~980 行）**  
   Synfacts 编排已迁出（~500 行 → `web/synfacts/reverse_examples.py`），但 bridge 重标、对称警告、比对逻辑仍堆在同一文件。

6. **缩写 `CN`（已修默认）**  
   上游表曾把 `CN` 覆盖成异腈 `[N+]#[C-]`，展开后 CIP 会翻。现默认强制腈 `C#N`，见 [`../translator/README.md`](../translator/README.md)。

---

## 未来重构建议（按优先级）

1. **扩 `rendering`**  
   reverse / peel / rgroup preview 全部改为 `render_structure`（或明确的 `kind=`）；再把 `mol_to_png_*` 从 `assembly` 挪进 `rendering`（或 `rendering/_rdkit_png.py`），让 `assembly` 只做合分子。

2. **再瘦 `reverse_assembly`**  
   - 核：`reverse_assemble` + `assemble_reactant_from_substituents` + bridge/label 重标  
   - 旁路：`attach_expected_comparisons` / `structure_png_base64` → `chemistry/compare_reactants.py` 或直接由 viewer 调 `rendering` + `chem_util`  
   - 结果：考虑「核结果 dataclass」+「展示 hydrate」两层（展示层可留在 web）

3. **收 Step0 包边界（可选）**  
   `align` + `peel` + `candidates` + 薄 `decompose.step0_scaffold_map` 可视为子包 `chemistry/step0/`，外 API 不变。

4. **高亮 mode**  
   `render_structure(..., kind="highlight", atom_indices=…)`，消化 `candidates.mol_highlight_png`。

5. **不要做的**  
   - 不要把 `esmiles` 嵌进 `chemistry`  
   - 不要把 `web.synfacts.reverse_examples` 再 import 回化学核  
   - 不要为「目录整齐」长期保留 src 根 shim

成功标准（比「src 没代码」更准）：

- `chemistry` 里每个文件都能一句话说清化学职责  
- viewer / batch 编排不泄漏进 peel/assemble 核  
- 用户可见图只经过 `rendering`（高亮除外直至第 4 步）
