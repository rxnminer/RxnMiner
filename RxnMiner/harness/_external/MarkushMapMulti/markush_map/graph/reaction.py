from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Union, Tuple, Optional

from tqdm import tqdm

from .component import Component, MarkushComponent, COMPONENT_TYPE, get_products_reactants_atom_map
from .markush_graph import MarkushGraph, FitResult, RecoverResult
from .matcher import MatchResult
from .structure.mol_graph import MolGraph
from .structure.process import smiles_to_graph

REACTION_TYPE = List[COMPONENT_TYPE]
SYNFACT_TYPE = Dict[str, Union[List[REACTION_TYPE], List[COMPONENT_TYPE]]]


@dataclass(frozen=True)
class ReactionFitResult:
    input_reaction: Reaction
    product_idx: int
    mol_graph: MolGraph
    fit_result: FitResult
    output_reaction: Reaction

    def __repr__(self) -> str:
        text = ""
        text += "##### FIT RESULT #####\n"
        text += self.fit_result.__repr__() + "\n"
        text += f"##### REACTION (pid={self.product_idx}) #####\n"
        text += self.output_reaction.__repr__() + "\n"
        return text


@dataclass
class Reaction:
    reactants: List[Component]
    conditions: List[Component]
    products: List[Component]
    _dict_p2r_atom_map: Optional[Dict[Tuple[int, int], Dict[str, str]]] = None
    _dict_p2c_atom_map: Optional[Dict[Tuple[int, int], Dict[str, str]]] = None

    @classmethod
    def from_json(cls, d: REACTION_TYPE) -> Reaction:
        reactants, conditions, products = [], [], []
        for dd in d:
            if dd["type"] == "reactants":
                reactants.append(Component.from_json(dd))
            elif dd["type"] == "conditions":
                conditions.append(Component.from_json(dd))
            elif dd["type"] == "products":
                products.append(Component.from_json(dd))
        return Reaction(reactants, conditions, products)

    def __repr__(self) -> str:
        def _append(_name: str, components: List[Component]):
            nonlocal text
            if len(components):
                text += f"\t{_name}: ({len(components)})\n"
                for c in components:
                    text += f"\t\t{c.text} | {' '.join(c.relations)}\n"
                    if isinstance(c, MarkushComponent) and c.recover_result is not None:
                        text += f"\t\t\tfrom: {c.recover_result.input_graph.caption}\n"

        text = ""
        _append("Reactants", self.reactants)
        _append("Conditions", self.conditions)
        _append("Products", self.products)
        return text

    def get_p2r_atom_map(self, product_index: int, reactant_index: int) -> Dict[str, str]:
        if self._dict_p2r_atom_map is None:
            return {}
        return self._dict_p2r_atom_map.get((product_index, reactant_index), None)

    def get_p2c_atom_map(self, product_index: int, condition_index: int) -> Dict[str, str]:
        if self._dict_p2c_atom_map is None:
            return {}
        return self._dict_p2c_atom_map.get((product_index, condition_index), None)

    def update_atom_map(self):
        if self._dict_p2r_atom_map is not None and self._dict_p2c_atom_map is not None:
            return
        self._dict_p2r_atom_map, self._dict_p2c_atom_map = {}, {}
        prc_atom_map = get_products_reactants_atom_map(self.reactants + self.conditions, self.products)
        n_reactant = len(self.reactants)
        for p_idx, rc_atom_map in enumerate(prc_atom_map):
            for r_idx, atom_map in enumerate(rc_atom_map[:n_reactant]):
                self._dict_p2r_atom_map[(p_idx, r_idx)] = atom_map
            for c_idx, atom_map in enumerate(rc_atom_map[n_reactant:]):
                self._dict_p2c_atom_map[(p_idx, c_idx)] = atom_map

    def to_smiles_fit_copy(self, smiles: str, relations: List[str], verbose: bool = False) -> ReactionFitResult:
        smiles_products: List[MarkushComponent] = [c for c in self.products if isinstance(c, MarkushComponent)]
        markush_products = [c for c in smiles_products if c.is_markush]
        if len(markush_products) == 1:
            product = markush_products[0]
        elif len(smiles_products) == 1:
            product = smiles_products[0]
        else:
            raise ValueError(f"Reaction.get_smiles_fit_copy: "
                             f"has products {len(markush_products)} / {len(smiles_products)} / {len(self.products)}")

        product_idx = self.products.index(product)

        mol_graph, atom_idx_to_id = smiles_to_graph(smiles)
        fit_result = product.markush_graph.fit_mol_graph(mol_graph, verbose=verbose)
        if fit_result is None:
            raise ValueError("Reaction.get_smiles_fit_copy: no match found")
        match_result = fit_result.match_result
        # if match_result.has_flexible_fractures():
        self.update_atom_map()

        def _recover(_c: Component, _result: MatchResult, _map: Optional[Dict[str, str]]) -> Component:
            if isinstance(_c, MarkushComponent):
                return _c.to_recover_copy(_result, _map, remove_additional_ring_sites=False)
            return _c

        def _recover_product(_c: Component, _result: MatchResult) -> Component:
            if _c is product:
                assert isinstance(_c, MarkushComponent)
                _g =  MarkushGraph(mol_graph)
                return MarkushComponent(smiles, relations, _g, RecoverResult(match_result, _c.markush_graph, _g))
            if isinstance(_c, MarkushComponent):
                return _c.to_recover_copy(_result, None)
            return _c

        return ReactionFitResult(self, product_idx, mol_graph, fit_result, Reaction(
            [_recover(c, match_result, self.get_p2r_atom_map(product_idx, i)) for i, c in enumerate(self.reactants)],
            [_recover(c, match_result, self.get_p2c_atom_map(product_idx, i)) for i, c in enumerate(self.conditions)],
            [_recover_product(c, match_result) for i, c in enumerate(self.products)]
        ))


@dataclass
class Synfact:
    reactions: List[Reaction]
    examples: List[Component]

    @classmethod
    def from_json(cls, d: SYNFACT_TYPE) -> Synfact:
        d_general_reactions: List[REACTION_TYPE] = d["general_reactions"]
        d_examples: List[COMPONENT_TYPE] = d["examples"]
        return Synfact(
            [Reaction.from_json(d_reaction) for d_reaction in d_general_reactions],
            [Component.from_json(d_example) for d_example in d_examples],
        )

    def __repr__(self) -> str:
        text = f"General Reactions: ({len(self.reactions)})\n"
        for reaction in self.reactions:
            text += f"\t{reaction.__repr__()}\n"
        text += f"Examples: ({len(self.examples)})\n"
        for example in self.examples:
            text += f"\t{example.__repr__()}\n"
        return text

    def fit_reactions_with_examples(
            self,
            use_tqdm: bool = False,
            debug: bool = False
    ) -> Dict[Tuple[int, int], ReactionFitResult]:
        dict_reaction_fit_result: Dict[Tuple[int, int], ReactionFitResult] = {}
        for i, reaction in enumerate(self.reactions):
            t = enumerate(self.examples)
            if use_tqdm:
                t = tqdm(t, total=len(self.examples), ncols=60)
            for j, example in t:
                try:
                    example_smiles = example.text
                    example_relations = example.relations
                    if "*" not in example_smiles:
                        reaction_fit_result = reaction.to_smiles_fit_copy(
                            example_smiles, example_relations, verbose=False)
                        dict_reaction_fit_result[(i, j)] = reaction_fit_result
                except Exception as e:
                    if debug:
                        raise e
                    print(f"Synfact.fit_reactions_with_examples: reaction {i} example {j} encounters "
                          f"{e.__class__.__name__}({e})")
        return dict_reaction_fit_result

