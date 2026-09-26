from dataclasses import dataclass
from typing import List, Dict

from rdkit import Chem

from .atom import Atom
from .base import IdentifiedObject
from .bond import Bond, BondType
from .mol_graph import MolGraph
from .process import graph_to_mol, smiles_to_graph


@dataclass
class MolFracture(IdentifiedObject):
    graph: MolGraph
    outer_bonds: List[Bond]

    @classmethod
    def from_case(
            cls,
            graph: MolGraph, related_bonds: List[Bond],
            complete_aid_to_skeleton_aid: Dict[str, str],
    ) -> 'MolFracture':
        atom_ids = set(graph.atoms.keys())
        outer_bonds = []
        for b in related_bonds:
            source, target = b.get_source_target(atom_ids)
            outer_bonds.append(Bond(b.id, source, complete_aid_to_skeleton_aid[target], b.type, b.properties))
        return MolFracture("", graph, outer_bonds)

    @classmethod
    def from_smiles(cls, smiles: str, target_id: str = "") -> 'MolFracture':
        graph, atom_map = smiles_to_graph(smiles)
        return MolFracture(f"-{smiles}", graph, [Bond("", atom_map[0], target_id, BondType.SINGLE)])

    def __repr__(self) -> str:
        return f"MolFracture({Chem.MolToSmiles(self.get_graph_with_stars())})"

    def __str__(self) -> str:
        return f"{Chem.MolToSmiles(self.get_graph_with_stars())}"

    def __eq__(self, other: 'MolFracture') -> bool:
        return len(self.outer_bonds) == len(other.outer_bonds) and self.graph.atom_bag == other.graph.atom_bag

    def __lt__(self, other: 'MolFracture') -> bool:
        if len(self.outer_bonds) == len(other.outer_bonds):
            return self.graph.atom_bag < other.graph.atom_bag
        return len(self.outer_bonds) < len(other.outer_bonds)

    def get_graph_with_stars(self) -> MolGraph:
        atom_ids = set(self.graph.atoms.keys())
        graph = self.graph.copy()
        for bond in self.outer_bonds:
            source = bond.get_source_target(atom_ids)[0]
            atom = Atom("", "*")
            graph.add_atom(atom)
            graph.add_bond(Bond("", source, atom.id, bond.type))
        return graph_to_mol(graph)[0]

    def deepcopy(self) -> 'MolFracture':
        graph, atom_id_map = self.graph.deepcopy()
        return MolFracture("", graph, [
            Bond("", atom_id_map[b.source], b.target, b.type, b.properties) for b in self.outer_bonds
        ])


@dataclass
class AtomSub(IdentifiedObject):
    atom_id: str
    origin_symbol: str
    sub_symbol: str

    def __repr__(self) -> str:
        return f"AtomSub({self.origin_symbol}->{self.sub_symbol})"

    def __str__(self) -> str:
        return f"{self.origin_symbol}->{self.sub_symbol}"


def get_fracture_ports_atom_dict(fractures: List[MolFracture]) -> Dict[str, List[str]]:
    dict_fracture_ports_atom: Dict[str, List[str]] = {}
    for f in fractures:
        atom_ids = set(f.graph.atoms.keys())
        ports_atom = [b.get_source_target(atom_ids)[1] for b in f.outer_bonds]
        dict_fracture_ports_atom[f.id] = ports_atom
    return dict_fracture_ports_atom
