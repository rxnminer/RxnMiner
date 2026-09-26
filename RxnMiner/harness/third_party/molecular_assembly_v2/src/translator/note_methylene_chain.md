# 亚甲基重复 `CH2?k`（Translator 扩链）

MolParser / Synfacts 常见标签：`CH2?3`、`CH2?9`、`CH2?n`（见 eSMILES 2.0「重复基团」与 annotation 数据）。

## 语义

| 标签 | 含义 | 能否变成具体原子 |
|------|------|------------------|
| `CH2?3` | 该 `*` 位点展开为 **3 个亚甲基** | 能（数字 k） |
| `CH2?9` | 同上，9 个 | 能 |
| `CH2?n` / `CH2?m-n` | 抽象或范围 | **不应**展开成定长 |

整串写作 `<a>{pos}:CH2?3</a>`。caption 文法里应对应 `symbol=CH2` + `multiple=3`。

与 `|Sg:n|` / `<g>` 整段聚合物重复不是同一层；这里只谈 **位点上的 (CH₂)ₖ**。

## 本仓库生产路径（已合入）

| 步骤 | 行为 |
|------|------|
| `load_esmiles` | 能 load；`display_label` → `(CH2)3` / `(CH2)n`（`short_display_from_source`） |
| `expand_numeric_ch2_in_esmiles` | 数字 k：薄适配（记邻居 → 删 `*` → 插链 → 接回） |
| `expand_template_abbreviations` | **先** CH2?k，**再** abbrev graft；`CH2?n` 保留且**不**回退坏掉的 `refactor` |
| `abbrevs.csv` | 仍无 `CH2?…` 项（正确：不是表内缩写） |

实现：`src/esmiles/methylene.py`。演示与边界：`notebooks/01-1_ch2_methylene_expand_dev.ipynb`。包文档：`src/esmiles/README.md`「亚甲基重复」。

桥位例：`C=C*CO` + `CH2?3` → `C=CCCCCO`（7 atoms）。

**边界**：不覆盖高分子 `|Sg:n|` / `<g>` / 通用双齿单体（缩写库无带两 `*` 的配套单体）；**不接手性**——业务上几乎不会把带手性的 C 写进 `(CH2)n`，为 CH2 做立体适配不现实。

## Translator 意图（`refactor` 里 `is_methylene`）——仅作对照

1. 认出 `CH2`（或 `CH`+script`2`）
2. `count = int(multiple)`，非数字则默认 **1**
3. 当前 `*` **就地改成 C**，清芳香/手性
4. `count > 1`：度 1 末端延长 / 度 2 桥插入

### 上游坑：数字 `?k` 实际经常没被解析

`Patterns.grp_content` 写在 **`rf"..."`** 里，其中的 `\d{1}` 被 f-string 吃成字面 `\d1`，故 `CH2?3` 的 multiple 常为空，refactor 往往只做 `*`→C。

**结论：可学插碳思路；不要依赖 `Translator.refactor` 做数字/抽象策略。**

## 自研薄适配（与生产一致）

1. 解析：`^CH2\?(\d+)$`；抽象 `CH2?n` / `?m-n` / 字母 → 不展开
2. 与 graft **同流水线**（先 CH2 再 graft）
3. `*`→删掉再插 k 个 C（勿残留同位素占位）
4. 立体：当前不做（见上边界）
