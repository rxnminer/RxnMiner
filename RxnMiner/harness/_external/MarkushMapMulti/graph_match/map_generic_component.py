import networkx as nx
from collections import Counter
from utils import find_mcs_mapping



def find_best_candidate(query_graph: nx.Graph,
                        candidates: list[nx.Graph],
                       ) -> tuple[int, float, dict]:
    """
    计算 MCS，最后返回 (best_idx, best_ratio, best_mapping)。
    """
    best_idx, best_ratio, best_map = -1, 0, {}
    qn = query_graph.number_of_nodes()
    # 列举原子符号
    common_symbols = [
        'H',  'He', 'Li', 'Be', 'B',  'C',  'N',  'O',  'F',  'Ne',
        'Na', 'Mg', 'Al', 'Si', 'P',  'S',  'Cl', 'Ar', 'K',  'Ca',
        'Sc', 'Ti', 'V',  'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu', 'Zn',
        'Ga', 'Ge', 'As', 'Se', 'Br', 'Kr', 'Rb', 'Sr', 'Y',  'Zr',
        'Nb', 'Mo', 'Tc', 'Ru', 'Rh', 'Pd', 'Ag', 'Cd', 'In', 'Sn',
        'Sb', 'Te', 'I',  'Xe', 'Cs', 'Ba', 'La', 'Ce', 'Pr', 'Nd',
        'Pm', 'Sm', 'Eu', 'Gd', 'Tb', 'Dy', 'Ho', 'Er', 'Tm', 'Yb',
        'Lu', 'Hf', 'Ta', 'W',  'Re', 'Os', 'Ir', 'Pt', 'Au', 'Hg',
        'Tl', 'Pb', 'Bi', 'Po', 'At', 'Rn', 'Fr', 'Ra', 'Ac', 'Th',
        'Pa', 'U',  'Np', 'Pu', 'Am', 'Cm', 'Bk', 'Cf', 'Es', 'Fm',
        'Md', 'No', 'Lr', 'Rf', 'Db', 'Sg', 'Bh', 'Hs', 'Mt', 'Ds',
        'Rg', 'Cn', 'Nh', 'Fl', 'Mc', 'Lv', 'Ts', 'Og'
    ]
    for i, Gc in enumerate(candidates):
        cn = Gc.number_of_nodes()
        r_cnt = 0
        for node, attrs in Gc.nodes(data=True):
            if attrs['symbol'].startswith('$'):
                cn -= 1
            elif attrs['symbol'] not in common_symbols:
                cn -= 1
                r_cnt += 1
        if r_cnt == 0 or cn >= qn:
            # 没有r基团，或者候选图的原子数大于等于查询图，一定不是骨架
            continue
        # if cn > 30:
        #     # 过大的候选图，可能会导致计算时间过长
        #     continue
        mapping = find_mcs_mapping(query_graph, Gc)
        # 计算重合比例
        ratio = len(mapping)
        if ratio > best_ratio:
            best_idx, best_ratio, best_map = i, ratio, mapping

    return best_idx, best_ratio, best_map
