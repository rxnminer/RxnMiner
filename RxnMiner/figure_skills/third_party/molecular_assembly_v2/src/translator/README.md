# translator (vendored)

Local copy of the upstream Translator distribution.

- `translator.py` — caption parse / `refactor()` abbreviation expansion
- `_utils.py` — slim utils (abbrev table, `merge_group`); no Node/SVG deps
- `abbrevs.csv` — abbreviation → SMILES mapping
- `abbrevs_extra.csv` — local additions (fill misses only)

笔记（读代码用）：

- [`note_caption_grammar.md`](note_caption_grammar.md) — `<a>` 标签怎么拆、为何不是 O+Tf 切词
- [`note_merge_group.md`](note_merge_group.md) — `merge_group` 逐步组装与手性**不**保证；对照本仓库 graft/assembly
- [`note_methylene_chain.md`](note_methylene_chain.md) — `CH2?k` 亚甲基扩链；本地尚未进 graft

Used by [`esmiles.abbrev`](../esmiles/abbrev.py) before Step0 `load_esmiles` / 卡片渲染. Do not import from `projects_on_git` at runtime.

Update by re-copying from upstream when the Translator project changes; then re-check the policies below.

## 缩写策略（本仓库覆盖）

### `CN` = 腈（默认），不考虑异腈

文献 / OCSR / Synfacts 里的 **`CN` 一律按腈（cyano）** 展开为 `C#N`。

上游 `abbrevs.csv` 里同一 `symbol=CN` 出现两次：

| 位置 | SMILES | 含义 |
|------|--------|------|
| 前段 | `C#N` | 腈（正确默认） |
| 后段（反向书写表） | `[N+]#[C-]` | 异腈 |

表加载若用后写覆盖，会把 `CN` 变成异腈，导致展开后 CIP `(R)/(S)` 翻转、卡片图与逆装产物和原图对不上。

本仓库强制：

1. [`_utils.get_abbrev_smi`](_utils.py) 加载结束后写死 `_abbrev_smi["CN"] = "C#N"`
2. [`esmiles.abbrev._lookup_abbrev_smiles`](../esmiles/abbrev.py) 对标签 `CN` 直接返回 `C#N`
3. `translator.py` 里已有 `CN → C(#N)` 硬编码（`refactor` 路径）

**异腈不参与默认解析**；若将来真要支持，应使用独立 symbol（不要占用 `CN`）。
