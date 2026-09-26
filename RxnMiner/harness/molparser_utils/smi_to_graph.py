from rdkit import Chem
import networkx as nx
import matplotlib.pyplot as plt
from networkx.algorithms import isomorphism
from collections import Counter
import re

import sys
sys.path.append(r"E:\SI")

# from Translator.translator import Translator

_re_num_prefix = re.compile(r"^\d+_(.+)$")
_re_a_prefix    = re.compile(r"^a_(.+)$")
_re_paren_num   = re.compile(r"\(\d+\)")

def split_groups(groups: str):
    # print(groups)
    pattern = r"<(?P<type>r|a)>(?P<content>.*?)</(?P=type)>"
    matches = re.finditer(pattern, groups)
    a_groups = {}
    r_groups = {}

    for m in matches:
        type_ = m.group("type")
        text = m.group("content")
        # 使用冒号分隔，提取ind和content
        if ":" in text:
            ind, content = text.split(":", 1)
        else:
            ind, content = "", text
        if type_ == "r":
            r_groups[int(ind)] = content
        else:
            a_groups[int(ind)] = content
    return a_groups, r_groups

def smiles_to_graph(smiles: str) -> nx.Graph:
    # print(f"smiles_to_graph: {smiles}")
    if '<sep>' in smiles:
        groups_str = smiles.split('<sep>')[1]
        a_groups, r_groups = split_groups(groups_str)
        # TODO：将group处理为只保留字母和数字
        smi = smiles.split('<sep>')[0]
    else:
        a_groups, r_groups = {}, {}
        smi = smiles
    mol = Chem.MolFromSmiles(smi)
    Chem.Kekulize(mol, clearAromaticFlags=False)
    ring_info = mol.GetRingInfo().AtomRings()
    ring_replace_atoms = {}
    for ring_idx, r_group in r_groups.items():
        for atom in ring_info[ring_idx]:
            ring_replace_atoms[atom] = r_group
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smiles}")
    # 设置原子编号
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(atom.GetIdx())
    G = nx.Graph()
    ring_replace_cnt = 0
    for atom in mol.GetAtoms():
        idx = atom.GetIdx()
        symbol = atom.GetSymbol()
        ind = atom.GetAtomMapNum()
        # print(symbol, ind)
        if symbol == '*':
            symbol = 'a_' + a_groups.get(ind, '*')
        # 移除括号
        if 'r' in symbol.lower() and not 'pin' in symbol.lower():
            symbol = (symbol.replace('[', '').replace(']', '')
                      .replace('(', '').replace(')', '')
                      .replace('【', '').replace('】', '')
                      .replace('（', '').replace('）', ''))
        G.add_node(idx, symbol=symbol,
                         atom_idx=idx,
                         chiral_tag=str(atom.GetChiralTag()),
                         formal_charge=atom.GetFormalCharge(),
                         hyb=str(atom.GetHybridization()),
                         is_aromatic=atom.GetIsAromatic())
        if ring_replace_atoms.get(idx) is not None:
            G.add_node(idx+200, symbol=f"$r{ring_replace_cnt}_{ring_replace_atoms[idx]}", is_aromatic=False)
            ring_replace_cnt += 1
    for bond in mol.GetBonds():
        i = bond.GetBeginAtomIdx()
        j = bond.GetEndAtomIdx()
        # 当边两边都是is_aromatic时，设置为芳香键
        G.add_edge(i, j,
                   bond_type=str(bond.GetBondType()),
                   is_conj=bond.GetIsConjugated(),
                   stereo=str(bond.GetStereo()),
                   is_aromatic=G.nodes[i]["is_aromatic"] and G.nodes[j]["is_aromatic"])
    for atom in ring_replace_atoms.keys():
        G.add_edge(atom, atom + 200,
                   bond_type='RING_REPLACE',
                   is_conj=False,
                   stereo='STEREONONE')
    # 为Ar增加额外的属性，当一个原子是aromatic，且它有一个以上的不为aromatic的邻居时，它是一个Ar原子
    for node, attrs in G.nodes(data=True):
        if attrs["is_aromatic"] and \
           sum(1 for n in G.neighbors(node) if G.nodes[n]["is_aromatic"] == False) > 0:
            attrs["Ar"] = True
        else:
            attrs["Ar"] = False
    # 记录其邻居和键的计数，在匹配时要求邻居必须匹配上，a_和$r可作为通配符，
    for node, attrs in G.nodes(data=True):
        neighbors = [G.nodes[n]["symbol"] for n in G.neighbors(node)]
        for i, symbol in enumerate(neighbors):
            if symbol.startswith("a_") or symbol.startswith("$r"):
                neighbors[i] = "*"
        attrs["neighbors_count"] = Counter(neighbors)
    return G

def graph_to_smiles(G: nx.Graph) -> str:
    """
    将带属性的图重建为 RDKit Mol 再生成 SMILES（保留立体信息）
    要求：
      - G 的节点和边属性应与 smiles_to_graph 中一致
    """
    mol = Chem.RWMol()
    idx_map = {}  # 图节点 -> 新 Mol 原子索引

    group_map = {}
    group_cnt = 1
    # 添加原子
    for node, attrs in G.nodes(data=True):
        if attrs["symbol"].startswith("a_"):
            # 如果是原子组，则添加到 group_map 中
            group_map[node] = attrs["symbol"][2:]
            atom = Chem.Atom("*")
            # atom.SetAtomMapNum(group_cnt)
            group_cnt += 1
        elif attrs["symbol"].startswith('$r'):
            continue
        else:
            atom = Chem.Atom(attrs["symbol"])
        # 设置手性标签
        atom.SetChiralTag(getattr(Chem.rdchem.ChiralType, attrs["chiral_tag"]))
        atom.SetFormalCharge(int(attrs["formal_charge"]))
        atom.SetHybridization(getattr(Chem.rdchem.HybridizationType, attrs["hyb"]))
        atom.SetIsAromatic(bool(attrs["is_aromatic"]))
        atom.SetAtomMapNum(node)
        new_idx = mol.AddAtom(atom)
        idx_map[node] = new_idx

    # 添加键
    for u, v, attrs in G.edges(data=True):
        if attrs["bond_type"] == 'RING_REPLACE':
            # 跳过环替换键
            continue
        bt = getattr(Chem.rdchem.BondType, attrs["bond_type"])
        mol.AddBond(idx_map[u], idx_map[v], bt)
        # 设置共轭与立体
        bond = mol.GetBondBetweenAtoms(idx_map[u], idx_map[v])
        bond.SetIsConjugated(bool(attrs["is_conj"]))
        bond.SetStereo(getattr(Chem.rdchem.BondStereo, attrs["stereo"]))

    # 生成 SMILES
    mol = mol.GetMol()
    # 1) 更新属性缓存，不严格检查 valence
    mol.UpdatePropertyCache(strict=False)

    # 2) 强制 sanitize（catchErrors=True 可以让它返回错误代码而不抛异常）
    Chem.SanitizeMol(mol, catchErrors=True)

    # 3) 强制去芳香、用 Kekulé 形式
    Chem.Kekulize(mol, clearAromaticFlags=True)
    smi = Chem.MolToSmiles(mol, isomericSmiles=True, canonical=True)
    mol2 = Chem.MolFromSmiles(smi)
    ind2group = {}
    for ind, atom in enumerate(mol2.GetAtoms()):
        if atom.GetSymbol() != '*':
            atom.SetAtomMapNum(0)
            continue
        map_num = atom.GetAtomMapNum()
        ind2group[ind] = group_map[map_num]
        atom.SetAtomMapNum(0)

    smi = Chem.MolToSmiles(mol2, canonical=False, isomericSmiles=True, kekuleSmiles=False)
    # 添加原子组
    if len(group_map) > 0:
        smi += '<sep>'
        for idx, group in sorted(ind2group.items(), key=lambda x: x[1]):
            smi += f'<a>{idx}:{group}</a>'
    return smi


def visualize_graph(G):
    """
    直接可视化 NetworkX 图结构
    - 显示每个节点的原子符号（symbol）
    - 显示每条边的键类型（bond_type）及立体化学（stereo）
    """
    pos = nx.spring_layout(G)  # 生成节点布局
    # 节点标签：symbol 或节点索引
    node_labels = {n: f"{G.nodes[n].get('symbol', str(n))}({n})" for n in G.nodes()}

    # 绘制节点
    nx.draw(G, pos, with_labels=False, node_size=800)
    nx.draw_networkx_labels(G, pos, labels=node_labels, font_size=12)

    # 边标签：bond_type (+ stereo if 有)
    edge_labels = {}
    for u, v, attrs in G.edges(data=True):
        label = attrs.get('bond_type', '')
        stereo = attrs.get('stereo', '')
        if stereo and stereo != 'STEREONONE':
            label += f" ({stereo})"
        edge_labels[(u, v)] = label
    nx.draw_networkx_edge_labels(G, pos, edge_labels=edge_labels, font_size=10)

    plt.axis('off')
    plt.show()


def find_attachment_points(G_full, R):
    # 在 G_full 中找到与 R 的连接点
    pts = set()
    for u in R.nodes():
        for v in G_full.neighbors(u):
            if v not in R:
                pts.add((u, v))
    return pts  # 返回所有 R-node ↔ MCS-node 边

def counters_match_with_star(c1: Counter, c2: Counter) -> bool:
    # 1. 各自的通配符数量
    wild1 = c1.get('*', 0)
    wild2 = c2.get('*', 0)

    # 2. 取出非 '*' 的真实键及其计数
    real1 = {k: v for k, v in c1.items() if k != '*'}
    real2 = {k: v for k, v in c2.items() if k != '*'}

    # 3. 构造所有出现过的键集合
    keys = set(real1) | set(real2)

    # 4. 计算：要把 c1 补齐到 c2（real2>real1）的总差值，需要 c1 用多少通配符
    deficit1 = sum(max(0, real2.get(k, 0) - real1.get(k, 0)) for k in keys)
    #    要把 c2 补齐到 c1 的通配符需求
    deficit2 = sum(max(0, real1.get(k, 0) - real2.get(k, 0)) for k in keys)

    # 5. 只要两边的通配符都足够补各自的差值，就认为它们可以匹配
    return (deficit1 <= wild1) and (deficit2 <= wild2)

def node_match(a_attrs: dict, b_attrs: dict) -> bool:
    """节点匹配：原子符号一致，若有手性标签也要一致"""
    # Ar与芳香原子同构，来解决匹配错误的问题。注意最后要在mapping中删掉这对映射
    if ("a_" in a_attrs.get("symbol").lower() and "ar" in a_attrs.get("symbol").lower() and b_attrs.get("Ar", False)) or \
        ("a_" in b_attrs.get("symbol").lower() and "ar" in b_attrs.get("symbol").lower() and a_attrs.get("Ar", False)):
        return True
    sym_a = normalize_symbol(a_attrs.get("symbol"))
    sym_b = normalize_symbol(b_attrs.get("symbol"))

    # print(f"node_match: {sym_a} {sym_b}")

    if sym_a != sym_b:
        return False

    # if a_attrs.get('is_aromatic', False) != b_attrs.get('is_aromatic', False):
    #     return False
    # 比较邻居，*作为通配符
    a_neighbors = a_attrs.get("neighbors_count", {})
    b_neighbors = b_attrs.get("neighbors_count", {})
    if (not a_attrs.get("symbol").startswith('a_') and not a_attrs.get("symbol").startswith('a_') and
            not a_attrs.get("symbol").startswith('$r') and not a_attrs.get("symbol").startswith('$r')):
        # 对于非markush原子，比较它的邻居
        if not counters_match_with_star(a_neighbors, b_neighbors):
            return False
    return True

def edge_match(a_attrs: dict, b_attrs: dict) -> bool:
    """边匹配：键类型一致"""
    return a_attrs.get("bond_type") == b_attrs.get("bond_type") or (a_attrs.get("is_aromatic", False) and b_attrs.get("is_aromatic", False))

def are_isomorphic(G1: nx.Graph, G2: nx.Graph) -> bool:
    """判断两个子图在 node_match/edge_match 下是否同构。"""
    nm = isomorphism.GraphMatcher(
        G1, G2,
        node_match=node_match,
        edge_match=edge_match
    )
    return nm.is_isomorphic()

import random
from typing import Dict, Tuple, List, Set
def find_mcs_mapping(
    G1: nx.Graph,
    G2: nx.Graph
) -> Dict:
    """
    基于模积图＋最大团分支定界＋染色上界的最大公共子图映射。
    返回 dict：G1节点 -> G2节点
    """
    # 1. 构建候选对列表
    pairs: List[Tuple] = []
    for u in G1.nodes():
        for v in G2.nodes():
            if node_match(G1.nodes[u], G2.nodes[v]):
                # 不匹配环取代，除非G1或G2只有一个节点
                if not (G1.number_of_nodes() == 1 or G2.number_of_nodes() == 1):
                    if G1.nodes[u].get('symbol', '').startswith('$r') or G2.nodes[v].get('symbol', '').startswith('$r'):
                        continue
                pairs.append((u, v))
    n = len(pairs)

    # 2. 构建模积图邻接（只存兼容边）
    adj: Dict[int, Set[int]] = {i: set() for i in range(n)}
    for i in range(n):
        u1, v1 = pairs[i]
        for j in range(i + 1, n):
            u2, v2 = pairs[j]
            # 同一原图或目标图节点不能重复映射
            if u1 == u2 or v1 == v2:
                continue
            e1 = G1.has_edge(u1, u2)
            e2 = G2.has_edge(v1, v2)
            if e1 == e2:
                if not e1 or edge_match(G1.edges[u1, u2], G2.edges[v1, v2]):
                    adj[i].add(j)
                    adj[j].add(i)

    best_clique: List[int] = []

    def greedy_color(cands: List[int]) -> Dict[int, int]:
        """
        对候选集做贪心着色，返回 node->color
        上界 = 最大 color + 1
        """
        color_label: Dict[int, int] = {}
        color_classes: List[List[int]] = []
        for u in sorted(cands, key=lambda x: len(adj[x])):
            for c_idx, cls in enumerate(color_classes):
                if all((w not in adj[u]) for w in cls):
                    cls.append(u)
                    color_label[u] = c_idx
                    break
            else:
                color_classes.append([u])
                color_label[u] = len(color_classes) - 1
        return color_label

    def expand(clique: List[int], cands: List[int]):
        nonlocal best_clique
        # 上界剪枝：当前大小 + 着色上界 <= 最佳，则剪枝
        color_lbl = greedy_color(cands)
        ub = max(color_lbl.values(), default=-1) + 1
        if len(clique) + ub <= len(best_clique):
            return
        # 按色值从大到小遍历，优先扩展上界高的节点
        for u in sorted(cands, key=lambda x: color_lbl[x], reverse=True):
            # 进一步剪枝：如果即插即上界小于等于 best，也可跳过
            if len(clique) + color_lbl[u] + 1 <= len(best_clique):
                continue
            # 扩展 clique
            new_clique = clique + [u]
            if len(new_clique) > len(best_clique):
                best_clique = new_clique
            # 计算新候选集：邻接 u 且在 cands 中
            new_cands = [w for w in cands if w in adj[u] and w != u]
            expand(new_clique, new_cands)
            # 回溯：从候选集中移除 u
            cands.remove(u)

    # 启动搜索
    expand([], list(range(n)))

    # 构建最终映射
    mapping: Dict = {}
    for idx in best_clique:
        u, v = pairs[idx]
        mapping[u] = v
    # 处理之前为了更好的匹配添加的临时配对
    for u, v in list(mapping.items()):
        if G1.nodes[u].get('symbol', '') != G2.nodes[v].get('symbol', ''):
            mapping.pop(u)
    return mapping


def is_smiles(value: str):
    """
    判断是否为smiles
    """
    # if 'c' not in value.lower():
    #     # 至少有个碳原子吧
    #     return False
    # if len(value) < 7:
    #     return False
    # jiankun的奇妙格式
    if '<sep>' in value:
        value = value[:value.find('<sep>')]
    smiles_characters = {
        # 常见有机原子
        "B", "C", "N", "O", "P", "S", "F", "C", "l", "I", "H",
        # 芳香原子（小写）
        "c", "n", "o", "s",
        # 可能和金属原子一起出现的字符
        "A", "a", "M", "g", "e", "L", "i", "N", "K", "S", "T", "V", "r", "n", "o", "G", "u",
        "Z", "R", "b", "Y", "Z", "b", "h", "d", "f", "W",
        # 键类型
        "-", "=", "#", ":",
        # 分支和环结构
        "(", ")",
        # 立体化学
        "@", "@@",
        # 电荷
        "+", "-",
        # 特殊符号
        "/", "\\", "*", "%",
        # 方括号用于特殊原子或电荷状态
        "[", "]",
        # 用来表示环位置的数字
        "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
        "."
    }
    for c in value:
        if c not in smiles_characters:
            return False
    try:
        mol = Chem.MolFromSmiles(value)
        assert mol is not None
        return True
    except Exception as e:
        return False


from rdkit import Chem
from rdkit.Chem import Draw
import matplotlib.pyplot as plt
from PIL import Image, ImageDraw, ImageFont
import io, requests, base64
import cairosvg

def draw_mol(smiles):
    import os
    url = os.environ.get("RXNMINER_STRUCTURE_RENDER_URL", "")
    data = {"smi": smiles}
    try:
        if '<sep>' in smiles and len(smiles.split('<sep>')[-1]) != 0:
            if not url:
                raise ValueError("Optional structure renderer is not configured")
            response = requests.post(url, json=data, timeout=30)
            svg = response.json()['data']['svg']
            base64_data = base64.b64encode(svg.encode('iso-8859-1')).decode('iso-8859-1')
        else:
            smiles_clean = smiles.replace('<sep>', '')
            mol = Chem.MolFromSmiles(smiles_clean)
            drawer = Draw.rdMolDraw2D.MolDraw2DSVG(300, 300)
            drawer.DrawMolecule(mol)
            drawer.FinishDrawing()
            svg = drawer.GetDrawingText()
            base64_data = base64.b64encode(svg.encode('utf-8')).decode('utf-8')
    except Exception as e:
        print(f"draw mol for {smiles} failed:", e)
        return None
    return base64_data

def pil_from_svg_base64(b64_svg: str) -> Image.Image:
    svg_data = base64.b64decode(b64_svg)
    png_bytes = cairosvg.svg2png(bytestring=svg_data)
    return Image.open(io.BytesIO(png_bytes))

def placeholder_image(text, size=(200,200)):
    """
    非 SMILES 文本的占位图：白底 + 黑字，居中显示 text。
    """
    img = Image.new("RGBA", size, "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", size=14)
    except IOError:
        font = ImageFont.load_default()
    # 计算文字边界，居中绘制
    bbox = draw.textbbox((0,0), text, font=font)
    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    x = (size[0] - tw) / 2
    y = (size[1] - th) / 2
    draw.text((x, y), text, font=font, fill="black")
    return img

def make_grid(images, legends=None, subimg_size=(200,200), mols_per_row=None):
    n = len(images)
    per_row = mols_per_row or n
    rows = (n + per_row - 1) // per_row
    w, h = subimg_size
    grid_w = per_row * w
    grid_h = rows * (h + 20)
    canvas = Image.new("RGBA", (grid_w, grid_h), "white")
    draw = ImageDraw.Draw(canvas)
    try:
        legend_font = ImageFont.truetype("DejaVuSans-Bold.ttf", size=12)
    except IOError:
        legend_font = ImageFont.load_default()

    for idx, img in enumerate(images):
        img_resized = img.resize(subimg_size, resample=Image.LANCZOS)
        x = (idx % per_row) * w
        y = (idx // per_row) * (h + 20)
        canvas.paste(img_resized, (x, y), img_resized.convert("RGBA"))
        if legends:
            legend = legends[idx]
            bbox = draw.textbbox((0,0), legend, font=legend_font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text((x + (w - tw) / 2, y + h), legend, font=legend_font, fill="black")
    return canvas

def visualize_reaction(reaction_data, img_path=None):
    # 1. 提取
    reactants = [e['text'] for e in reaction_data if e['type']=='reactants']
    products  = [e['text'] for e in reaction_data if e['type']=='products']
    # conditions = [f"{e['text']}({','.join(e.get('relations',[]))})"
    #               for e in reaction_data if e['type']=='conditions']
    yields = []
    for e in reaction_data:
        if e['type']=='products' and 'relation' in e:
            yields += e['relation']

    # 2. 生成 PIL 图像或占位
    def render_list(smiles_list):
        imgs = []
        for smi in smiles_list:
            if is_smiles(smi):
                b64 = draw_mol(smi)
                if b64:
                    imgs.append(pil_from_svg_base64(b64))
                else:
                    imgs.append(placeholder_image(smi))
            else:
                imgs.append(placeholder_image(smi))
        return imgs

    react_imgs = render_list(reactants)
    prod_imgs  = render_list(products)

    # 3. 拼网格
    pil_react = make_grid(react_imgs, legends=reactants, subimg_size=(200,200), mols_per_row=len(reactants))
    pil_prod  = make_grid(prod_imgs,  legends=products,  subimg_size=(200,200), mols_per_row=len(products))

    # 4. 箭头 & 5. 合并
    widthR, heightR = pil_react.size
    widthP, heightP = pil_prod.size
    h = max(heightR, heightP)
    arrow_w = int(h * 0.6)
    arrow = Image.new("RGBA", (arrow_w, h), (255,255,255,0))
    d = ImageDraw.Draw(arrow)
    try:
        arrow_font = ImageFont.truetype("DejaVuSans-Bold.ttf", size=int(h*0.5))
    except IOError:
        arrow_font = ImageFont.load_default()
    bbox = d.textbbox((0,0), "→", font=arrow_font)
    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    d.text(((arrow_w-tw)/2, (h-th)/2), "→", font=arrow_font, fill="black")

    total_w = widthR + arrow_w + widthP
    canvas = Image.new("RGBA", (total_w, h), "white")
    canvas.paste(pil_react, (0, (h-heightR)//2), pil_react)
    canvas.paste(arrow, (widthR, 0), arrow)
    canvas.paste(pil_prod, (widthR+arrow_w, (h-heightP)//2), pil_prod)

    # 6. 展示 & 注释
    plt.figure(figsize=(total_w/100, 6))
    plt.imshow(canvas)
    plt.axis('off')
    # plt.text(0.5, -0.05,
    #          "Yield/EE: " + (", ".join(yields) or "—"),
    #          transform=plt.gca().transAxes, ha='center')
    # 如需条件，取消下一行注释：
    # plt.text(0.5, -0.15, "\n".join(conditions), transform=plt.gca().transAxes, ha='center')
    plt.tight_layout()

    if img_path:
        plt.savefig(img_path, format='png', bbox_inches='tight', pad_inches=0.1)
        plt.close()

    else:
        plt.show()
        plt.close()






import matplotlib.pyplot as plt
import networkx as nx

def visualize_graph(G: nx.Graph, title: str | None = None):
    """
    可视化 smiles_to_graph 得到的 G：
      - 节点标签：symbol(idx)
      - 边标签：bond_type（芳香键标注 "ar"）
      - 不同类型节点用不同颜色：
          普通原子: lightblue
          a_* 可变基团: orange
          $r* 环替代节点: lightgreen
    """
    # 1. 布局
    pos = nx.spring_layout(G, seed=42)  # 固定 seed 方便对比

    # 2. 给不同类型的节点分组着色
    node_colors = []
    node_labels = {}
    for n, attrs in G.nodes(data=True):
        sym = attrs.get("symbol", str(n))
        node_labels[n] = f"{sym}({n})"

        if sym.startswith("$r"):
            node_colors.append("lightgreen")   # 环替代节点
        elif sym.startswith("a_"):
            node_colors.append("orange")       # Markush / 可变基团
        else:
            node_colors.append("lightblue")    # 普通原子

    # 3. 画节点 + 标签
    plt.figure(figsize=(8, 6))
    nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=800, edgecolors="black")
    nx.draw_networkx_labels(G, pos, labels=node_labels, font_size=8)

    # 4. 边标签：区分芳香键 / RING_REPLACE
    edge_labels = {}
    edge_colors = []
    for u, v, attrs in G.edges(data=True):
        bt = attrs.get("bond_type", "")
        if bt == "RING_REPLACE":
            label = "RING"
            edge_colors.append("red")
        else:
            if attrs.get("is_aromatic", False):
                label = "ar"   # 芳香键
                edge_colors.append("gray")
            else:
                label = bt
                edge_colors.append("black")
        edge_labels[(u, v)] = label

    nx.draw_networkx_edges(G, pos, edge_color=edge_colors, width=1.5)
    nx.draw_networkx_edge_labels(G, pos, edge_labels=edge_labels, font_size=7)

    if title:
        plt.title(title)

    plt.axis("off")
    plt.tight_layout()
    plt.show()



# Translator.refactor(example['text']).caption
# smi_test1 = Translator.refactor("*N1CCN2C(COCC3=C2N=CC=C3)C1<sep><a>0:<dum></a><r>2:R[7]</r>").caption
# smi_test2 = Translator.refactor("*N1CC2N(C3C(COC2)=CC=CN=3)CC1<sep><a>0:<dum></a><r>2:R[7]</r>").caption
# G1 = smiles_to_graph(smi_test1)
# G2 = smiles_to_graph(smi_test2)
# visualize_graph(G1, title=smi_test1)
# visualize_graph(G2, title=smi_test2)

from networkx.algorithms import isomorphism

import re

def normalize_symbol(sym: str) -> str:
    """
    去掉 symbol 中用于编号 / instance 区分的部分
    只保留“化学语义”
    """
    if sym is None:
        return sym

    # 1. 去掉括号里的纯数字，如 C(1), R(12)
    sym = re.sub(r"\(\d+\)", "", sym)

    # 2. 去掉前缀 $r, 例如 $r12_C → C
    sym = re.sub(r"\$r\d+_", "", sym)

    # 3. 去掉前缀 num_, 例如 1_C → C
    m = _re_num_prefix.match(sym)
    if m:
        sym = m.group(1)

    # 3. 去掉前缀 a_, 例如 a_R123 → R
    m = _re_a_prefix.match(sym)
    if m:
        sym = m.group(1)

    return sym


def graphs_equivalent(smi1: str, smi2: str) -> bool:
    G1 = smiles_to_graph(smi1)
    G2 = smiles_to_graph(smi2)
    gm = isomorphism.GraphMatcher(
        G1, G2,
        node_match=node_match,
        edge_match=edge_match
    )
    return gm.is_isomorphic()

# print("equivalent?", graphs_equivalent(G1, G2))
