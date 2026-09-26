# `merge_group`：怎么组装，手性怎么办

实现：[`_utils.merge_group`](_utils.py)。调用方：上游 `Translator.refactor`，以及本仓库 viewer 的 `/api/rgroup/preview`（UI 拼 R 片段预览）。

**一句话：** 把缩写表里的「无 `*` 基团 SMILES」接到骨架某个度=1 的 `*` 上；**只改连通性，不维护四面体手性奇偶。**

本仓库生产路径（`esmiles.abbrev` → `chemistry.graft` → `assembly`）**不走** `merge_group`，用手性可追踪的配对组装。下文先讲清 `merge_group`，再对比。

---

## 1. 约定：缩写 SMILES 的「0 号原子」是连接原子

表项例如：

| symbol | SMILES | 含义 |
|--------|--------|------|
| `Ph` | `c1ccccc1` | 苯环；**atom 0** 接到骨架 |
| `CN` | `C#N` / refactor 里常写成 `C(#N)` | **C** 接到骨架（腈，不是异腈 N） |
| `NPhth` | `N1C(=O)c2ccccc2C1=O` | **N** 接到骨架 |

`merge_group` **永远**把骨架邻居接到 `src` 的 **atom 0**（`idx_map[0]`）。  
写反（如腈写成 `N#C`）会接错端；这就是为什么本仓库强制 `CN → C#N`。

---

## 2. 组装步骤（逐步）

前置条件（不满足就抛错）：

1. `attach_idx` 上的原子必须是 `*`
2. `*` 恰好一个邻居 `nb`（度=1）
3. `*–nb` 必须是**单键**

然后（就地改 `RWMol`）：

```text
骨架:   ...—nb—*          缩写 src:  (0)—(1)—(2)—...
                 │                      ↑
              attach_idx            约定连接原子
```

1. `MolFromSmiles(src)` → `src_mol`
2. 把 `src_mol` **每个原子** `AddAtom` 进骨架（拷贝元素、电荷等；原子上的 `ChiralTag` 会跟着拷，若有）
3. 把 `src_mol` **每条键**按 `BondType` `AddBond`（**只拷键型，不拷 `BondDir` 楔形/虚线**）
4. `RemoveBond(nb, *)`
5. `AddBond(nb, idx_map[0], SINGLE)`  ← 骨架接到缩写的 0 号原子

```text
结果（此时 * 还在，但已悬空无键）:

  ...—nb—(0)—(1)—(2)—...
         \________/
          刚拷进来的缩写
  *  (孤立 dummy，等调用方删)
```

`merge_group` **自己不删 `*`**。`refactor` 里会把该 index 记入 `to_remove`，循环结束后再 `RemoveAtom`；viewer 预览路径则往往在 `MolToSmiles` 时丢掉孤立原子，或根本从「已带 `*` 的片段」起步。

单原子缩写（如 `Me → C`）在 `refactor` 里**不走** `merge_group`，而是直接把 `*` 的原子序数改成 C（原子突变）。

---

## 3. 手性：它保证了什么、没保证什么

### 3.1 它做了什么

| 对象 | 行为 |
|------|------|
| 缩写片段内部立体 | 若表 SMILES 带 `@`，`AddAtom` 可能带上该原子的 `ChiralTag`；键的楔形信息一般**没有** |
| 骨架上原有的 `@` | **不读、不写、不按邻居置换校正** |
| 删 `*` / 换邻居之后 | **没有**「期望邻居序 vs 实际邻居序 → 奇偶翻转」 |

### 3.2 为什么骨架手性会漂

四面体 `ChiralTag` 相对的是 **邻居在 RDKit 里的顺序**，不是 CIP R/S 标签。

替换前：

```text
手性碳 nb 的邻居序（示意）: [A, B, C, *]
ChiralTag = CW / CCW（相对该序）
```

替换后：

```text
邻居变成: [A, B, C, (0)]   或别的插入顺序
* 被删，atom index 重排
```

若不对「`*` → 伙伴」做一次 **置换奇偶** 判断并按需翻转 `ChiralTag`，同一相对构型可能被写成相反的 `@`/`@@`，CIP 图注也会跟着翻。

`merge_group` **完全没有这一步**。`refactor` 末尾还有：

```text
SanitizeMol → MolToSmiles(isomeric) → MolFromSmiles
```

这是一次 canonical 往返，可能「碰巧」看起来还对，也可能静默改立体——**不是**可证明的保立体协议。

### 3.3 可复现反例：`NPhth` 接到手性碳上的 `*`（对映体翻了）

仓库回归：[`tests/test_abbrev_expand.py`](../../tests/test_abbrev_expand.py) · `test_nphth_expand_preserves_absolute_stereo_via_assembly`。

输入（同一条 eSMILES）：

```text
CC[C@H](*)C=O<sep><a>3:NPhth</a>
```

表项：`NPhth → N1C(=O)c2ccccc2C1=O`（atom 0 = N 接到骨架）。

| 路径 | 导出 isomeric SMILES | CIP（AssignStereochemistry 后） | InChIKey 立体段 |
|------|----------------------|--------------------------------|-----------------|
| **graft / assembly**（`substitute_known_abbrev_slots_with_mol`） | `CC[C@@H](C=O)N1C(=O)c2ccccc2C1=O` | **S**，`ChiralTag=CW` | `…-QMMMGPOBSA-N` |
| **`merge_group` 裸调**（`*`→N 后删孤立 dummy，无 parity） | `CC[C@H](C=O)N1C(=O)c2ccccc2C1=O` | **R**，`ChiralTag=CCW` | `…-MRVPVSSYSA-N` |
| **`Translator.refactor`**（内部 `merge_group`） | 同上 `@` 写法 | **R** | 同上 |

连通性相同（同一平面结构 InChI 前缀），**立体绝对构型相反**——这就是「盲保留 `@` / 不校正邻居置换」的实锤，不是抽象担心。

为何会翻（直觉）：

```text
替换前:  手性碳邻居序 ≈ [Et, *, CHO]，tag = CCW（SMILES 写成 [C@H]）
merge:  * 换成 NPhth 的 N，邻居序变了，tag 仍当 CCW 用 → 物理构型反了
graft:  AtomMap 记下期望邻居序，删 dummy 后发现奇置换 → 翻转 tag → 写成 [C@@H]，CIP=S
```

本地复现：

```python
from translator import _utils as tu
from esmiles.abbrev import substitute_known_abbrev_slots_with_mol
from translator import Translator
from rdkit import Chem

line = "CC[C@H](*)C=O<sep><a>3:NPhth</a>"
_, graft_mol = substitute_known_abbrev_slots_with_mol(line, [])
ref = Translator.refactor(line, error_msg=False)
print(Chem.MolToSmiles(graft_mol, isomericSmiles=True))
print(ref.caption)  # 对映体
```

### 3.4 对 UI 预览意味着什么

`/api/rgroup/preview` 用 `merge_group` 生成的是：

- **真 eSMILES / 真连通性**（不是 Ketcher 图层假画）
- **适合看「接上了没有、接在哪一端」**
- **不适合**当作「与文献相对构型逐原子对齐」的权威结果

前后关系靠业务层绑：`R[1]` 标签 ↔ 这条片段 eSMILES，不是靠 `merge_group` 内部的 map 保立体。

---

## 4. 和本仓库生产路径对比（为何要弃用它做模板展开）

| | `merge_group`（Translator / UI 预览） | `chemistry.graft` + `assembly`（`substitute_known_abbrev_slots_with_mol`） |
|--|--------------------------------------|-----------------------------------------------------------------------------|
| 输入 | 骨架 RWMol + 无 `*` 的基团 SMILES | 骨架 eSMILES + 给基团加上 `*` 的 fragment 行 |
| 连接 | 就地 `AddAtom`/`AddBond`，钉死 atom 0 | 两个 `*` 端口配对（与 R-pair 组装同一核） |
| 手性 | 无邻居置换奇偶校正 | AtomMap 记下替换前邻居序，删 dummy 后按置换奇偶决定是否翻转 |
| 用途 | 上游 caption refactor；viewer 快速预览 | 模板缩写代入 → **真实参与后续反应的工作 Mol** |

生产路径示意：

```text
表: NPhth = N1C(=O)c2ccccc2C1=O
  → star_body_from_group_smiles → *N1C(=O)...
  → 与骨架 * 打上同一 pair 标签 __abbrev_k
  → build_complete_molecule（assembly）
  → 内存工作 Mol + encode 导出 eSMILES
```

结论：**取其缩写表与「atom 0 = 连接原子」约定；弃其 `merge_group` 作为有立体要求的组装实现。**

---

## 5. 读代码时的检查清单

看一段「代入」代码时问：

1. 连接原子是不是表 SMILES 的 **atom 0**？
2. `*` 是就地删，还是配对 assemble？
3. 删/换邻居后有没有 **parity / AtomMap 邻居序** 逻辑？
4. 有没有 `MolToSmiles`→`MolFromSmiles` 往返在「静默改立体」？

若答案是「只有 `merge_group` + Sanitize/往返」→ **不要**拿它当相对构型真值。
