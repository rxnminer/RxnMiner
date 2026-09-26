# MolParserUtils

一个用于解析、重构和可视化分子结构的工具包，支持 E-SMILES 格式（带 R 基团注释的扩展 SMILES）。

## 目录结构

```
molparserutils/
├── README.md                       # 项目说明文档
├── molparser_utils/                # 源代码目录
│   ├── __init__.py                 # 包初始化文件，导出主要接口
│   ├── translator.py               # 核心翻译和重构模块（SMILES）
│   ├── draw_esmiles.py             # 分子绘图模块
│   ├── smi_to_graph.py             # 分子转图结构模块
│   ├── translator_to_cxsmiles.py   # 核心翻译和重构模块 (CXSMILES)
│   ├── compare_esmiles.py          # 分子比对模块
│   └── utils/                      # 工具函数模块
│       ├── __init__.py             # 工具函数导出
│       └── abbrevs.csv             # 缩写基团数据文件
│   └── test/                       # 单元测试目录
│       ├── __init__.py             # 工具函数导出
│       └── test_compare.py/        # 分子比对单元测试模块
```

## 功能特性

### 1. 分子翻译与重构 (`translator.py` & `translator_to_cxsmiles.py`)

- **E-SMILES 解析**：解析包含 R 基团注释的扩展 SMILES 字符串
- **缩写基团展开**：将缩写基团（如 `OMe`, `Et` 等）展开为完整结构
- **Markush 结构识别**：自动识别 Markush 结构和 SRU（结构重复单元）
- **索引映射**：在重构后自动更新原子和环的索引
- **Markush 代入**：将 R 基团的映射关系代入 Markush 生成分子

### 2. 分子绘图 (`draw_esmiles.py`)

- **SVG/PNG 渲染**：支持生成 SVG 和 PNG 格式的分子结构图
- **可配置样式**：提供丰富的绘图配置选项
- **立体化学保留**：在绘图过程中保留分子的立体化学信息
- **R 基团标注**：支持抽象环(<c>)标注、环上连接线（<r>）等高级特性

### 3. 分子比对（`compare_esmiles.py`）

- **判别分子相同**：判断2个分子是否相同
- **分子相似度计算**：计算2个分子的3D空间相似度和2D平面相似度
- **分子指纹计算**：计算分子的Morgan Fingerprint
- **图结构相似度计算**：支持对两个图结构的点和边相似度计算

## 快速开始

### 基本使用

#### 解析和重构

```python
from molparser_utils import Translator, draw, draw_esmiles

# 解析和重构 E-SMILES
caption = "*CCO<sep><a>0:OMe</a>"
result = Translator.refactor(caption)

print(f"重构后的 SMILES: {result.smi}")
print(f"是否为 Markush 结构: {result.markush}")
print(f"是否为 SRU 结构: {result.sru}")

# 绘制分子结构
svg = draw(caption)
with open("molecule.svg", "w") as f:
    f.write(svg)

# 绘制同时输出 PIL Image 和 SVG
image, svg_content = draw_esmiles("*C(O)c1cc(C(=O)N(*)*)cc(-c2*ccc*2)c1<sep><a>0:CF3</a><a>9:R[3]</a><a>10:R[2]</a><a>14:X</a><a>18:Y</a><r>1:R[1]?1-3</r>", dpi=300)
```

#### Markush 代入

```python
from molparser_utils import Translator

esmiles="*CCC<sep><a>0:R[21.1]</a>"

#注意substitution_map中R基团的名字和esmiles中R基团名字一致
substitution_map={"R[21.1]":"NO"}

result=Translator.markush_substitute(esmiles,substitution_map)
print(f"代入后的 SMILES: {result.smi}")


```

#### 分子比对
```python
from molparser_utils.compare_esmiles import MoleculeComparator
from molparser_utils.translator_to_cxsmiles import Translator

comparator = MoleculeComparator(Translator)
esmiles1="*c1cc2c(s1)CN(*)C2<sep><a>0:<dum></a><a>8:<dum></a>"
esmiles2="*C1SC2CN(CC=2C=1)*<sep><a>0:<dum></a><a>9:<dum></a>"
result=comparator.compare(esmiles1, esmiles2)
    
state=result.state.value
similarity_chirality=result.similarity_chirality
similarity_nochirality=result.similarity_nochirality

print(f"esmiles1: {esmiles1}")
print(f"esmiles2: {esmiles2}")
print(f"similarity_chirality:{similarity_chirality}, similarity_nochirality:{similarity_nochirality}")
print(f"compare result: {state}")
```


## API 文档

### Translator 类

核心翻译和重构类。

#### `Translator.refactor(caption: str, error_msg: bool = False) -> TranslatedMolecule`

重构 E-SMILES 字符串，展开缩写基团。

E-SMILES 规则详见： https://arxiv.org/abs/2411.11098

**参数：**
- `caption` (str): E-SMILES 格式的分子描述，格式为 `SMILES<sep>groups`
- `error_msg` (bool): 是否在出错时输出错误信息

**返回：**
- `TranslatedMolecule`: 包含以下字段的数据类
  - `smi` (str): 重构后的 SMILES 字符串
  - `groups` (str): 更新后的 R 基团描述（仅当存在 Markush 结构时）
  - `caption` (str): 完整的分子描述
  - `markush` (bool): 是否为 Markush 结构
  - `sru` (bool): 是否为 SRU 结构

**示例：**
```python
result = Translator.refactor("*CCO<sep><a>0:OMe</a>")
print(result.smi)  # 输出展开后的 SMILES
```

#### `Translator.parse_caption(caption: str, return_mol: bool = False, error_msg: bool = False)`

解析完整的分子描述字符串。

**参数：**
- `caption` (str): E-SMILES 格式的分子描述
- `return_mol` (bool): 是否返回 RDKit Mol 对象
- `error_msg` (bool): 是否输出错误信息

**返回：**
- `Tuple[Union[Mol, str], str, str]`: (SMILES/Mol, groups, extension)

### 绘图函数

#### `draw(smi: str, config: Optional[DrawingConfig] = None) -> str`

绘制分子结构并返回 SVG 字符串。

**参数：**
- `smi` (str): SMILES 或 E-SMILES 字符串
- `config` (DrawingConfig, 可选): 绘图配置对象

**返回：**
- `str`: SVG 字符串

#### `draw_esmiles(esmi: str, dpi: int = 300) -> Tuple[Image.Image, str]`

绘制 E-SMILES 并返回 PIL Image 对象和 SVG 字符串。

**参数：**
- `esmi` (str): E-SMILES 字符串
- `dpi` (int): 渲染分辨率

**返回：**
- `Tuple[Image.Image, str]`: (PIL Image 对象, SVG 字符串)

### MoleculeComparator 类

 
分子对比

####  `MoleculeComparator.compare(esmiles1: str, esmiles2: str, replace_R: bool = False) -> ComparisonResult`

**参数：**
- `esmiles1` (str): ESMILES of the first molecule to be compared
- `esmiles2` (str): ESMILES of the second molecule to be compared
- `replace_R` (bool): if they are replaced <a></a> and <r></r> with R. Default is False


**返回：**
- `ComparisonResult`: 包含以下字段的数据类
  - `esmiles1` (str): 待比对的第一个分子
  - `esmiles2` (str): 待比对的第二个分子
  - `state` (ComparisonState): 比对结果
    - SAME = 'same_mol'
    - DIFF_R = 'mol_with_diff_R_name'
    - DIFF_S = 'mol_with_diff_stereo'
    - DIFF_B = 'different_backbone'
    - INVALID = 'invalid_smiles'
  - `similarity_chirality` (float): 两个分子在3维空间上的相似度
  - `similarity_nochirality` (float): 两个分子在2维空间上的相似度
  - `message` (str): 提示信息

## 数据文件

### `utils/abbrevs.csv`

包含常用化学基团缩写及其对应的 SMILES 表示。文件格式：
- `name`: 基团名称
- `smiles`: SMILES 表示（默认最左侧为连接点）
- `atoms`: 原子数量（去H）
- `symbol`: 缩写符号（E-SMILES中识别出的，需要被带入的缩写符号）
- `label`: 绘制标签
- `labelW`: 绘制标签反转（即连接点在右侧时的写法）

## E-SMILES 格式说明

E-SMILES 是扩展的 SMILES 格式，支持 R 基团注释：

```
SMILES<sep>groups
```

其中 `groups` 部分包含 R 基团描述：

- 原子基团：`<a>index:content</a>`
- 环上连接基团：`<r>index:content</r>`
- 抽象环：`<c>index:content</c>`
- 连接点：`<a>index:<dum></a>`

## 开发指南

### 代码组织

- **`translator.py`**: 核心解析和重构逻辑，包含共享的基础类
- **`draw_esmiles.py`**: 绘图功能，从 `translator.py` 导入共享类
- **`utils/__init__.py`**: 工具函数，包括缩写基团处理、分子操作等

### 添加新的缩写基团

编辑 `utils/abbrevs.csv` 文件，添加新行：

```csv
name,smiles,atoms,symbol,label,labelW
NewGroup,CCCC,4,NewG,NewG,NewG
```

### 扩展绘图功能

在 `draw_esmiles.py` 中的 `DrawingTranslator.reconstruct_mol` 方法中添加新的绘图逻辑。


## 贡献指南

### PR 提交规则

**提交前必读：** 请确保遵循以下规范，以保证代码质量和项目维护效率。

#### Commit 和 PR 消息格式

提交消息必须使用以下格式：

- **新特性提交**：`[feat] 描述你的功能`
- **Bug 修复**：`[bug] 描述修复的问题`

**示例：**
```
[feat] 添加分子结构可视化功能
[bug] 修复虚拟原子索引映射错误
```

#### 分支命名规范

分支命名必须遵循以下格式：

- **新特性分支**：`feat/your_name/feature_info`
  - 示例：`feat/xfang/dynamic_res`
  
- **Bug 修复分支**：`bug/your_name/fix_bug_info`
  - 示例：`bug/xfang/fix_typo`


#### 提交流程

- 在独立分支上开发
- 提交 PR 前，在独立分支执行 `git rebase master` 同步最新代码
- 向 `main` / `master` 分支提交 PR


#### 提交前检查清单

提交前请确保：

- 代码符合 PEP 8 语法规范
- 通过所有 pytest 单元测试
- 保持线性 Git 历史（使用 rebase 而非 merge）
- 完善相关文档和注释
- 更新 CHANGELOG（若有）
