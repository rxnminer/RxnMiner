import json
import re
from dataclasses import dataclass, asdict
from typing import List, Dict, Tuple

import networkx as nx

from map_generic_component import find_best_candidate
from reconstruct_reaction import find_Rp_R_mapping, apply_all_R_groups_with_counts
from utils import smiles_to_graph, graph_to_smiles, find_mcs_mapping
from Translator.translator import Translator
from literal.parse_r_group_eg import parse_r_group_nexus


@dataclass(frozen=True)
class Connection:
    label: str
    general_mcs_attach_point: int
    example_mcs_attach_point: int
    r_group_attach_point: int


@dataclass(frozen=True)
class RGroup:
    smiles: str
    connections: List[Connection]


@dataclass(frozen=True)
class RMappingResult:
    general_smiles: str
    example_smiles: str
    mapping: Dict[int, int]
    r_groups: List[RGroup]

    @classmethod
    def from_mapping(cls, general_smiles: str, example_smiles: str) -> 'RMappingResult':
        gs = smiles_to_graph(general_smiles)
        es = smiles_to_graph(example_smiles)
        mapping = find_mcs_mapping(es, gs)
        r_group_jsons = find_Rp_R_mapping(es, gs)
        r_groups: List[RGroup] = []
        for r_group_json in r_group_jsons:
            rp_group_json = r_group_json["Rp_group"]
            if rp_group_json is None:
                continue
            rp_subgraph_smiles = graph_to_smiles(rp_group_json["Rp_subgraph"])
            if "ring_replace" in r_group_json.keys():
                label = r_group_json["ring_replace"]["symbol"]
                connections = [Connection(
                    label,
                    rp_attach_json["general_mcs_attach_point"],
                    rp_attach_json["example_mcs_attach_point"],
                    rp_attach_json["Rp_attach_point"],
                ) for rp_attach_json in rp_group_json["attachment"]]
            else:
                r_subgraph_smiles = graph_to_smiles(r_group_json["R_subgraph"])
                labels = re.findall(r"<a>\d+:([^<]+)</a>", r_subgraph_smiles)
                connections = [Connection(
                    labels[i],
                    rp_attach_json["general_mcs_attach_point"],
                    rp_attach_json["example_mcs_attach_point"],
                    rp_attach_json["Rp_attach_point"],
                ) for i, rp_attach_json in enumerate(rp_group_json["attachment"])]
            r_groups.append(RGroup(rp_subgraph_smiles, connections))
        return RMappingResult(
            graph_to_smiles(gs), graph_to_smiles(es),
            mapping,
            r_groups,
        )

    def to_dict(self) -> Dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def refine_general_reaction_with_examples(
        reactants_smiles: List[str],
        products_smiles: List[str],
        examples_smiles: List[str],
) -> List[Tuple[List[str], List[str]]]:
    reactants_graph = [smiles_to_graph(s) for s in reactants_smiles]
    products_graph = [smiles_to_graph(s) for s in products_smiles]
    examples_graph = [smiles_to_graph(s) for s in examples_smiles]
    if len(products_graph) == 1:
        main_product_id = 0
    else:
        main_product_ids = [find_best_candidate(g, products_graph)[0] for g in examples_graph]
        main_product_ids = set(main_product_ids)
        if len(main_product_ids) != 1:
            raise ValueError(f"refine_general_reaction_with_examples: multiple match {main_product_ids}")
        main_product_id = main_product_ids.pop()

    refined_reactions: List[Tuple[List[str], List[str]]] = []
    for example_graph in examples_graph:
        main_product_graph = products_graph[main_product_id]
        r_groups = find_Rp_R_mapping(example_graph, main_product_graph)
        new_reactants_graph = [apply_all_R_groups_with_counts(
            g, r_groups, main_product_graph
        )[0][0] for g in reactants_graph]
        new_products_graph = [apply_all_R_groups_with_counts(
            g, r_groups, main_product_graph
        )[0][0] if i != main_product_id else example_graph for i, g in enumerate(products_graph)]
        for graph in (new_reactants_graph + new_products_graph):
            if nx.number_connected_components(graph) > 1:
                raise ValueError("refine_general_reaction_with_examples: "
                                 "New graph has multiple connected components, matching failed.")
        new_reactants_smiles = [graph_to_smiles(g) for g in new_reactants_graph]
        new_products_smiles = [graph_to_smiles(g) for g in new_products_graph]
        refined_reactions.append((new_reactants_smiles, new_products_smiles))
    return refined_reactions


translator = Translator()


def replace_batch(strings: List[str], label: str, parts: List[str]) -> List[str]:
    new_strings: List[str] = []
    for s in strings:
        if label not in s:
            new_strings.append(s)
            continue
        for part in parts:
            new_strings.append(s.replace(label, part))
    return new_strings


def fill_general_smiles_with_r_group_dict(
        general_smiles: str,
        r_group_dict: Dict[str, List[str]],
) -> List[str]:
    strings = [general_smiles]
    for k, vs in r_group_dict.items():
        strings = replace_batch(strings, k, vs)
    strings = [translator.refactor(s).caption for s in strings]
    return strings


def fill_general_smiles_with_texts(
        general_smiles: str,
        texts: List[str],
) -> List[Tuple[str, List[str]]]:
    results = parse_r_group_nexus(texts)
    tuples_smiles_relations: List[Tuple[str, List[str]]] = []
    for result in results:
        smiles_list = fill_general_smiles_with_r_group_dict(general_smiles, result.dict_r_group)
        for smiles in smiles_list:
            tuples_smiles_relations.append((smiles, result.relations))
    return tuples_smiles_relations


def process_r_group_json(d: Dict) -> Tuple[Dict, bool]:
    valid = False
    d_examples = d["examples"]
    new_d_examples = []
    for d_eg in d_examples:
        relations, text = d_eg["relations"], d_eg["text"]
        if text.endswith("<sep>") or "<sep>" not in text:
            new_d_examples.append(d_eg)
            continue
        tuples_smiles_relations = fill_general_smiles_with_texts(text, relations)
        if len(tuples_smiles_relations) > 1:
            valid = True
        for new_smiles, new_relations in tuples_smiles_relations:
            new_d_examples.append({"relations": new_relations, "text": new_smiles})
    dd = d.copy()
    dd["examples"] = new_d_examples
    return dd, valid


if __name__ == '__main__':
    # _result = RMappingResult.from_mapping(
    #     "*C(*)c1ccccc1<sep><a>0:R[1]</a><a>2:R[3]</a><r>0:R[2]</r>",
    #     "N2NNCC(C2)c1cc(O)c(N)cc1",
    # )
    # print(_result.general_smiles)
    # print(_result.example_smiles)
    # print(_result.mapping)
    # for _g in _result.r_groups:
    #     print(f"\t{_g.smiles}")
    #     for _c in _g.connections:
    #         print(f"\t\t{_c}")
    # print(_result.to_json())
    # _rss = [
    #     "Nc1ccccc1<sep><r>0:R[2]</r>",
    #     "*C(*)C(O)=O<sep><a>0:R[1]</a><a>2:R[3]</a>",
    # ]
    # _pss = [
    #     "C=C",
    #     "*C(*)c1ccccc1<sep><a>0:R[1]</a><a>2:R[3]</a><r>0:R[2]</r>",
    # ]
    # _ess = [
    #     "N2NNCC(C2)c1cc(O)c(N)cc1",
    # ]
    # _result = refine_general_reaction_with_examples(_rss, _pss, _ess)
    # for _r in _result:
    #     _rrss, _rpss = _r
    #     print("REACTANTS:")
    #     for _rrs in _rrss:
    #         print(_rrs)
    #     print("PRODUCTS:")
    #     for _rps in _rpss:
    #         print(_rps)
    _result = fill_general_smiles_with_texts(
        "*C=C*<sep><a>0:R1</a><a>3:R2</a>",
        ["(1.0 equiv)", "R1=Ph,Et R2=nPr"],
        # {"R1": ["Ph", "Et"], "R2": ["nPr"]}
    )
    for _r in _result:
        print(_r)
