from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Union, Tuple, Optional

from .markush_graph import MarkushGraph, RecoverResult
from .matcher import MatchResult
from .structure.process import graph_to_smiles
from .utils.rules import is_markush
from .utils.rxn_map import get_smiles_unique_map

COMPONENT_TYPE = Dict[str, Union[str, List[str]]]


@dataclass(frozen=True)
class Component:
    text: str
    relations: List[str]

    @classmethod
    def from_json(cls, d: COMPONENT_TYPE) -> Component:
        text, relations = d["text"], d["relations"]
        if is_markush(text):
            markush_graph, _ = MarkushGraph.from_caption(text)
            return MarkushComponent(text, relations, markush_graph)
        return Component(text, relations)

    @property
    def smiles(self) -> str:
        return ""

    def get_smiles_with_map(self) -> Tuple[str, Dict[str, int]]:
        return "", {}

    def __repr__(self) -> str:
        return f"Component(text={self.text}, relations={' '.join(self.relations)})"

@dataclass(frozen=True)
class MarkushComponent(Component):
    markush_graph: MarkushGraph
    recover_result: Optional[RecoverResult] = None

    @property
    def is_markush(self) -> bool:
        return len(self.markush_graph.get_all_sites()) > 0

    @property
    def smiles(self) -> str:
        return graph_to_smiles(self.markush_graph.get_fix_star_graph()[0])[0]

    def get_smiles_with_map(self) -> Tuple[str, Dict[str, int]]:
        return graph_to_smiles(self.markush_graph.get_fix_star_graph()[0])

    def __repr__(self) -> str:
        return f"MarkushComponent(text={self.text}, relations={' '.join(self.relations)})"

    def to_recover_copy(
            self,
            match_result: MatchResult,
            atom_id_map: Optional[Dict[str, str]],
            remove_additional_ring_sites: bool = False,
    ) -> MarkushComponent:
        # print(self.markush_graph.caption)
        recover_result = self.markush_graph.recover(match_result, atom_id_map)
        output_graph = recover_result.output_graph
        if remove_additional_ring_sites:
            output_graph.remove_ring_sites()
        # print(new_markush_graph.caption)
        return MarkushComponent(output_graph.caption, self.relations, output_graph, recover_result)


def get_products_reactants_atom_map(
        reactants: List[Component],
        products: List[Component],
) -> List[List[Dict[str, str]]]:
    r_tuples_smiles_map = [c.get_smiles_with_map() for c in reactants]
    p_tuples_smiles_map = [c.get_smiles_with_map() for c in products]
    reactants_dict_idx_to_unique, products_dict_idx_to_unique = get_smiles_unique_map(
        [s for s, _ in r_tuples_smiles_map],
        [s for s, _ in p_tuples_smiles_map],
    )
    products_reactants_atom_map = []
    for product_index in range(len(products)):
        product_idx_to_unique = products_dict_idx_to_unique[product_index]
        product_map = p_tuples_smiles_map[product_index][1]
        product_map_inv: Dict[int, str] = {v: k for k, v in product_map.items()}
        product_unique_to_idx: Dict[int, int] = {v: k for k, v in product_idx_to_unique.items()}
        reactants_atom_map: List[Dict[str, str]] = [{} for _ in range(len(reactants))]
        for i, reactant_idx_to_unique in enumerate(reactants_dict_idx_to_unique):
            reactant_map = r_tuples_smiles_map[i][1]
            reactant_map_inv: Dict[int, str] = {v: k for k, v in reactant_map.items()}
            for atom_id, unique_id in reactant_idx_to_unique.items():
                if unique_id in product_unique_to_idx.keys():
                    reactant_atom_id = reactant_map_inv[atom_id]
                    product_atom_id = product_map_inv[product_unique_to_idx[unique_id]]
                    reactants_atom_map[i][product_atom_id] = reactant_atom_id
        products_reactants_atom_map.append(reactants_atom_map)
    return products_reactants_atom_map
