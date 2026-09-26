import networkx as nx
import copy

from utils import find_mcs_mapping, find_attachment_points, are_isomorphic
from networkx.algorithms import isomorphism
from collections import Counter


def find_Rp_R_mapping(G_example: nx.Graph, G_general: nx.Graph):
    # 提取MCS映射
    mapping = find_mcs_mapping(G_example, G_general)  # key是general, value是example

    # 取出 general 中不在 mapping 里的节点作为 R
    R_nodes = set(G_general.nodes()) - set(mapping.values())
    R_subgraphs = [G_general.subgraph(comp).copy()
                   for comp in nx.connected_components(G_general.subgraph(R_nodes))]

    # 同理对 example 拆 R′
    Rp_nodes = set(G_example.nodes()) - set(mapping.keys())
    Rp_subgraphs = [G_example.subgraph(comp).copy()
                    for comp in nx.connected_components(G_example.subgraph(Rp_nodes))]

    # 找到所有Rp的子结构和连接点
    Rp_groups = []
    for Rp_subgraph in Rp_subgraphs:
        attach_points = find_attachment_points(G_example, Rp_subgraph)
        attachment = []
        general_pts = []
        for rp_attach_point, example_mcs_attach_point in attach_points:
            general_mcs_attach_point = mapping[example_mcs_attach_point]
            bond_info = G_example.edges[rp_attach_point, example_mcs_attach_point]
            attachment.append({
                'Rp_attach_point': rp_attach_point,
                'example_mcs_attach_point': example_mcs_attach_point,
                'general_mcs_attach_point': general_mcs_attach_point,
                'bond_info': bond_info,
            })
            # 保留原始顺序，包括重复
            general_pts.append(general_mcs_attach_point)

        Rp_groups.append({
            'Rp_subgraph': Rp_subgraph,
            'attachment': attachment,
            'general_mcs_attach_points': general_pts  # 列表，允许重复
        })

    # 找到所有R的子结构和连接点
    R_groups = []
    for R_subgraph in R_subgraphs:
        attach_points = find_attachment_points(G_general, R_subgraph)
        attachment = []
        general_pts = []
        for r_attach_point, general_mcs_attach_point in attach_points:
            attachment.append({
                'R_attach_point': r_attach_point,
                'general_mcs_attach_point': general_mcs_attach_point,
            })
            general_pts.append(general_mcs_attach_point)

        R_groups.append({
            'R_subgraph': R_subgraph,
            'attachment': attachment,
            'general_mcs_attach_points': general_pts  # 列表，允许重复
        })

    # 环化反应：合并多点attach的R
    def merge_R_groups_for_Rp(R_attach_map, target_pts):
        # 使用Counter比较多重集合
        target_counter = Counter(target_pts)
        chosen = []
        current = Counter()
        # 贪心选R
        for idx, pts in R_attach_map.items():
            # 计算覆盖量
            pts_counter = Counter(pts)
            cover = sum((pts_counter & Counter(target_pts)).values())
            # 选最大
            if cover > 0 and sum((current & target_counter).values()) < sum(target_counter.values()):
                chosen.append(idx)
                current += pts_counter
                if all(current[p] >= target_counter[p] for p in target_counter):
                    break
        # 检查是否完全覆盖
        if all(current[p] >= target_counter[p] for p in target_counter):
            return chosen
        return []

    # 构建 R_idx -> attach pts 列表
    R_attach_map = {i: R['general_mcs_attach_points'] for i, R in enumerate(R_groups)}

    to_remove = set()
    new_Rs = []

    for Rp in Rp_groups:
        S = Rp['general_mcs_attach_points']
        if len(S) <= 1:
            continue
        chosen = merge_R_groups_for_Rp(R_attach_map, S)
        if not chosen:
            continue
        # 合并子图
        merged = nx.Graph()
        merged_attach = []
        merged_attachment = []
        for idx in chosen:
            R = R_groups[idx]
            merged_attach.extend(R['general_mcs_attach_points'])
            merged_attachment.extend(R['attachment'])
            for n, attrs in R['R_subgraph'].nodes(data=True):
                merged.add_node(n, **attrs)
            for u, v, attrs in R['R_subgraph'].edges(data=True):
                merged.add_edge(u, v, **attrs)
            to_remove.add(idx)

        new_Rs.append({
            'R_subgraph': merged,
            'general_mcs_attach_points': merged_attach,
            'attachment': merged_attachment
        })

    # 更新 R_groups
    final_R = [R for i, R in enumerate(R_groups) if i not in to_remove] + new_Rs
    R_groups = final_R

    # 匹配 R 和 Rp
    """
    记录一下这里R和Rp匹配的逻辑
    候选集：每个R可能匹配的Rp的列表
    匹配集：确定的R-Rp的关系
    待匹配集合：还没有确定匹配关系的R集合
    执行如下逻辑
    1. 将所有R加入到待匹配集合中
    2. 对于每个R，根据general_mcs_attach_point确定其候选集（即可能是R的对应的Rp，可能有多个）
    3. 对于每个待匹配集合中且候选集长度为1的R，将匹配关系加入到匹配集中，从待匹配集合中删除R
    4. 对于匹配集中的每个R-Rp，
        对于待匹配集合中每个基团R1
            对于其候选集中的每个Rp1，若Rp1与Rp重构，则：
                若R与R1重构，将R1-Rp1加入到匹配集中，从待匹配集合中删除R1
                若R与R1不重构，从R1的候选集中删除Rp1
    5. 重复234直到待匹配集合为空，或者在某一次循环中没有新的匹配关系加入到匹配集中
    6. 若待匹配集合仍不为空，选择待匹配集合中的一个R，随机选择其候选集中的一个Rp，将R-Rp加入到匹配集中，从待匹配集合中删除R，然后重复345
    """
    unmatched = set(range(len(R_groups)))
    matched = {}
    # 候选集：使用Counter比较多重集合
    candidates = {}
    for i, R in enumerate(R_groups):
        candidates[i] = [j for j, Rp in enumerate(Rp_groups)
                         if Counter(R['general_mcs_attach_points']) == Counter(Rp['general_mcs_attach_points'])]
        if not candidates[i]:
            unmatched.discard(i)
            # 没有匹配到任何Rp_group，匹配为None（Rp应该为氢）
            matched[i] = None

    def remove_candidate(i):
        for j in list(unmatched):
            if i in candidates.get(j, []):
                candidates[j].remove(i)

    def propagate():
        changed = False
        # 单候选
        for i in list(unmatched):
            if len(candidates.get(i, [])) == 1:
                matched[i] = candidates[i][0]
                unmatched.remove(i)
                # 从其他人的candidates里面删去，避免重复匹配
                remove_candidate(candidates[i][0])
                changed = True
            elif len(candidates.get(i, [])) == 0:
                matched[i] = None
                unmatched.remove(i)
                changed = True
        # 基于结构同构消除
        for i, rp_i in list(matched.items()):
            if rp_i is None:
                continue
            R_i = R_groups[i]['R_subgraph']
            Rp_i = Rp_groups[rp_i]['Rp_subgraph']
            for j in list(unmatched):
                to_remove = []
                for rp_j in candidates.get(j, []):
                    Rp_j = Rp_groups[rp_j]['Rp_subgraph']
                    if not nx.is_isomorphic(Rp_i, Rp_j):
                        continue
                    R_j = R_groups[j]['R_subgraph']
                    if nx.is_isomorphic(R_i, R_j):
                        matched[j] = rp_j
                        unmatched.remove(j)
                        remove_candidate(rp_j)
                        changed = True
                    else:
                        to_remove.append(rp_j)
                for bad in to_remove:
                    candidates[j].remove(bad)
                    changed = True
        return changed

    while propagate():
        pass

    # 剩余随机匹配
    while unmatched:
        j = unmatched.pop()
        if candidates.get(j):
            matched[j] = candidates[j][0]
            remove_candidate(candidates[j][0])
        else:
            matched[j] = None
        propagate()

    # 记录匹配
    for i, j in matched.items():
        R_groups[i]['Rp_group'] = Rp_groups[j] if j is not None else None

    # 处理一下环取代，将被取代的原子总是替换成最小的那个
    for R_group in R_groups:
        if len(R_group['R_subgraph'].nodes) == 1:
            symbol = list(R_group['R_subgraph'].nodes(data=True))[0][1]['symbol']
            if symbol.startswith('$r'):
                idx = symbol[2:]
                idx = int(idx.split('_')[0])
                symbol = symbol.split('_')[1]
                R_group['ring_replace'] = {
                    'idx': idx,
                    'symbol': symbol
                }
        else:
            continue
    # 过滤R_group，如果都是基本原子，则认为是一个误写，不去匹配
    common_symbols = [
        'H', 'He', 'Li', 'Be', 'B', 'C', 'N', 'O', 'F', 'Ne',
        'Na', 'Mg', 'Al', 'Si', 'P', 'S', 'Cl', 'Ar', 'K', 'Ca',
        'Sc', 'Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu', 'Zn',
        'Ga', 'Ge', 'As', 'Se', 'Br', 'Kr', 'Rb', 'Sr', 'Y', 'Zr',
        'Nb', 'Mo', 'Tc', 'Ru', 'Rh', 'Pd', 'Ag', 'Cd', 'In', 'Sn',
        'Sb', 'Te', 'I', 'Xe', 'Cs', 'Ba', 'La', 'Ce', 'Pr', 'Nd',
        'Pm', 'Sm', 'Eu', 'Gd', 'Tb', 'Dy', 'Ho', 'Er', 'Tm', 'Yb',
        'Lu', 'Hf', 'Ta', 'W', 'Re', 'Os', 'Ir', 'Pt', 'Au', 'Hg',
        'Tl', 'Pb', 'Bi', 'Po', 'At', 'Rn', 'Fr', 'Ra', 'Ac', 'Th',
        'Pa', 'U', 'Np', 'Pu', 'Am', 'Cm', 'Bk', 'Cf', 'Es', 'Fm',
        'Md', 'No', 'Lr', 'Rf', 'Db', 'Sg', 'Bh', 'Hs', 'Mt', 'Ds',
        'Rg', 'Cn', 'Nh', 'Fl', 'Mc', 'Lv', 'Ts', 'Og'
    ]
    to_remove = []
    for i, R_group in enumerate(R_groups):
        # 检查 R_subgraph 是否只包含基本原子
        if all(R_group['R_subgraph'].nodes[n]['symbol'] in common_symbols for n in R_group['R_subgraph'].nodes()):
            to_remove.append(i)
    # 删除这些 R_group
    to_remove = set(to_remove)
    R_groups = [R for i, R in enumerate(R_groups) if i not in to_remove]
    return R_groups


def substitute_one_R(G: nx.Graph, R_group: dict) -> list[nx.Graph]:
    """
    在 G 中，把 R_group['R_subgraph'] 的所有同构依次替换成 R_group['Rp_group']['Rp_subgraph']。
    如果 R_group['Rp_group'] is None，则只删除 R_subgraph 对应的节点，不插入任何原子。
    返回所有可能的替换结果图列表。
    """
    R = R_group['R_subgraph']
    Rp_group = R_group.get('Rp_group')
    Rp = Rp_group['Rp_subgraph'] if Rp_group else None
    attach_R = R_group['attachment']
    attach_Rp = Rp_group['attachment'] if Rp_group else None

    GM = nx.algorithms.isomorphism.GraphMatcher(
        G, R,
        node_match=lambda a, b: a.get('symbol') == b.get('symbol'),
        edge_match=lambda a, b: a.get('bond_type') == b.get('bond_type')
    )
    results = []

    for mapping in GM.subgraph_isomorphisms_iter():
        # R_node -> G_node
        inv_map = {r: g for g, r in mapping.items()}

        # —— 第一步：记录每个 R_attach_point 在 G 中的 attach points
        attach_info = {}
        for aR in attach_R:
            rnode = aR['R_attach_point']
            g_r = inv_map[rnode]
            neighbors = [
                nbr for nbr in G.neighbors(g_r)
                if nbr not in inv_map.values()
            ]
            attach_info[rnode] = neighbors

        # —— 第二步：拷贝并删除 R 对应的节点
        G_new = copy.deepcopy(G)
        G_new.remove_nodes_from(inv_map.values())

        # 如果没有 Rp_group，直接返回删掉后的图
        if Rp is None:
            results.append(G_new)
            continue

        # —— 第三步：插入 Rp_subgraph 的节点和边
        idx_map = {}
        next_idx = max(G_new.nodes, default=-1) + 1
        for rp_n, attrs in Rp.nodes(data=True):
            idx_map[rp_n] = next_idx
            G_new.add_node(next_idx, **attrs)
            next_idx += 1
        for u, v, attrs in Rp.edges(data=True):
            G_new.add_edge(idx_map[u], idx_map[v], **attrs)

        # —— 第四步：重连 attachment，按 general_mcs_attach_point 一一对应
        # 先把 R 和 Rp 都按 m 分组
        R_map, Rp_map = {}, {}
        for aR in attach_R:
            R_map.setdefault(aR['general_mcs_attach_point'], []).append(aR)
        for aRp in attach_Rp:
            Rp_map.setdefault(aRp['general_mcs_attach_point'], []).append(aRp)

        # 对每个 m，把 R_list 和 Rp_list 按顺序 zip，然后重连
        for m, R_list in R_map.items():
            Rp_list = Rp_map.get(m, [])
            for aR, aRp in zip(R_list, Rp_list):
                rnode = aR['R_attach_point']
                rpnode = aRp['Rp_attach_point']
                bond_info = aRp['bond_info']  # 例如 {'bond_type': 'SINGLE', ...}
                for g_attach in attach_info.get(rnode, []):
                    G_new.add_edge(g_attach, idx_map[rpnode], **bond_info)

        results.append(G_new)

    return results


def apply_all_R_groups_with_counts(G_A: nx.Graph,
                                   R_groups: list[dict],
                                   G_template: nx.Graph  # G_template是得到R_groups的那个通式，用于找到环取代的位点
                                   ) -> list[tuple[nx.Graph, int]]:
    """
    对 G_A 依次应用每个 R_group 的替换，并记录每条分支上总共做了多少次替换。
    返回一个 (graph, count) 的列表，按 count 降序排列。
    """
    # 初始：只有原图，且替换次数为 0
    if G_A.number_of_nodes() > 35:
        # 如果图节点数超过35，可能会导致计算时间过长，直接返回原图
        return [(G_A, 0)]
    graphs = [(G_A, 0)]
    # 先处理环取代
    has_ring_replace = False
    for R_group in R_groups:
        if 'ring_replace' in R_group:
            has_ring_replace = True
            break

    if has_ring_replace:
        def find_ring_replace_neighbor(G, node):
            # 遍历node的邻居
            neighbors = list(G.neighbors(node))
            for nbr in neighbors:
                if G.nodes[nbr].get('symbol', '').startswith('$r'):
                    return nbr
            return None

        new_R_groups = copy.deepcopy(R_groups)
        mapping = find_mcs_mapping(G_A, G_template)
        for A_atom, template_atom in mapping.items():
            ring_replace_nbr = find_ring_replace_neighbor(G_A, A_atom)
            if ring_replace_nbr is None:
                continue
            A_symbol = G_A.nodes[ring_replace_nbr]['symbol']
            template_ring_replace_nbr = find_ring_replace_neighbor(G_template, mapping[A_atom])
            template_symbol = G_template.nodes[template_ring_replace_nbr]['symbol']
            if A_symbol == template_symbol:
                continue
            # print(f"Replacing ring {A_symbol} with {template_symbol} in R_groups")
            target_R_group = None
            src_R_group = None
            for R_group in new_R_groups:
                if 'ring_replace' in R_group:
                    if f"$r{R_group['ring_replace']['idx']}_{R_group['ring_replace']['symbol']}" == A_symbol:
                        target_R_group = R_group
            for R_group in R_groups:
                if 'ring_replace' in R_group:
                    if f"$r{R_group['ring_replace']['idx']}_{R_group['ring_replace']['symbol']}" == template_symbol:
                        src_R_group = R_group
            target_R_group['Rp_group'] = src_R_group['Rp_group']
            if target_R_group['Rp_group'] is not None:
                for i, aRp in enumerate(target_R_group['Rp_group']['attachment']):
                    aRp['general_mcs_attach_point'] = target_R_group['attachment'][i]['general_mcs_attach_point']
        R_groups = new_R_groups

    for Rg in R_groups:
        new_graphs = []
        for G, cnt in graphs:
            replaced = substitute_one_R(G, Rg)
            if replaced:
                # 每个替换分支，都要把替换次数 +1
                for g_new in replaced:
                    new_graphs.append((g_new, cnt + 1))
            else:
                # 如果这一组没有任何匹配，保留原图，次数不变
                new_graphs.append((G, cnt))
        graphs = new_graphs

    # 最后按匹配次数降序
    graphs.sort(key=lambda x: x[1], reverse=True)
    return graphs
