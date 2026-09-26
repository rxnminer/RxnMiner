# Caption 文法（Translator）

上游 OCSR / caption 把「骨架 SMILES + 位点标签」写成一行字符串。本文件只说明 **标签怎么拆**，不涉及化学接枝（见 [`note_merge_group.md`](note_merge_group.md)）。

## 整行长什么样

```text
{body SMILES}<sep>{groups}{|extension|}
```

例：

```text
*C[C@@H](C(=O)*)*<sep><a>0:R</a><a>5:NHQ</a><a>6:NPhth</a>
```

| 段 | 含义 |
|----|------|
| `body` | RDKit 可解析的 SMILES（可含 `*`、`@`） |
| `<sep>` | 固定分隔符 |
| `groups` | 零个或多个 `<a>…</a>` / `<c>…</c>` / `<r>…</r>` |
| `\|…\|` | 可选 extension（如 SRU `Sg:n`）；本仓库很少用 |

`<a>{原子索引}:{标签内容}</a>` 里的索引是 **body 写成 SMILES 时的原子位置**（与 RDKit `MolFromSmiles` 后的 atom index 在简单情况下常一致，但 refactor 里会用 AtomMap 再对齐）。

## 一条 `<a>` 标签内容怎么拆

实现：`Patterns.grp_content` + `get_group_texts`（`translator.py`）。

```text
{SYMBOL}{[SCRIPT]}{PRIME}{?MULTIPLE}
```

正则字段：

| 字段 | 模式（意译） | 例子 |
|------|----------------|------|
| **symbol** | `[A-Za-z0-9-()]*` 整段 | `OTf`、`CO2H`、`NPhth`、`t-Bu`、`NO`、`R` |
| **script** | 可选 `[…]`，括号去掉后留下标 | `NO[2]` → script=`2` |
| **prime** | 可选 `'` / `"` | `R'` |
| **multiple** | 可选结尾 `?n` / `?a` / `?1-3` | `CH2?3` |

拆完得到 `GroupDesc(symbol, script, prime, multiple, …)`。

### 查表键怎么拼（refactor）

```text
lookup = symbol
若有 script:
  combined = symbol + script          # "NO" + "2" → "NO2"
  若 combined ∈ abbrevs 或周期表 → lookup = combined
```

然后 `abbrevs.csv[lookup]` → 基团 SMILES。

**没有**「把 `OTf` 切成 `O`+`Tf`」的化学 tokenizer。

| 标签原文 | symbol | script | 查表键 |
|----------|--------|--------|--------|
| `OTf` | `OTf` | — | `OTf`（整词） |
| `CO2H` | `CO2H` | — | `CO2H`（数字在 symbol 里） |
| `NO[2]` | `NO` | `2` | `NO2`（script 拼合） |
| `NPhth` | `NPhth` | — | `NPhth` |
| `R[1]` | `R` | `1` | 通常不在表 → Markush |
| `t-Bu` | `t-Bu` | — | `t-Bu`（连字符允许） |

保证不乱切的方式：**整词 ∈ 表**，不是智能分词。表里没有的整词就当开放 Markush，不会猜成碎片。

## `tokenize_*` 是干什么的

`tokenize_symbol`（按字符切开）、`tokenize_smiles`、`tokenize_caption` 等是给 **caption 序列化 / 训练式 token / 画标签** 用的，**不是**缩写查表的切词器。查表路径不走「最长匹配切基团名」。

## 和本仓库 Synfacts 标签的关系

MolParser / Synfacts 写出的 `<a>` 多半已是 **整词**（`NPhth`、`t-Bu`、`CN`），很少是 `NO[2]` 这种「主符号+下标」形式。

因此本仓库主路径 `esmiles.abbrev._lookup_abbrev_smiles` 更简单：

```text
标签整串 →（开放 Markush 则跳过）→ 精确 / 大小写不敏感查表
```

不解析 script/prime/multiple。caption 文法只在 `Translator.refactor` 回退路径里完整生效。

## 开放 Markush vs 可展开缩写

refactor 里大致：

1. 查到 abbrev → 单原子突变或 `merge_group`
2. 查到周期表元素且无 multiple → `*` 变成该元素
3. 否则 → 真 Markush，保留标签

本仓库还会显式把 `R` / `R[n]` / `Ar` / `Alk` / `NHQ` 等当开放槽（见 `esmiles.abbrev._is_open_markush_label`），即使表里偶然有同名也不展开。
