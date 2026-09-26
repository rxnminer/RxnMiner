import random
import string
from dataclasses import dataclass
from functools import reduce
from typing import List, Tuple, Dict, Optional, Set, Union

import numpy as np
import numpy.random as npr
import pandas as pd
from rdkit import Chem

from markush_map.graph.utils.detect_ring import find_ring_atoms
from markush_map.graph.markush_graph import MarkushGraph
from markush_map.graph.matcher import MatchResult, FixPair, RingPair
from markush_map.graph.structure.mol_fracture import MolFracture, AtomSub
from markush_map.graph.structure.process import mol_to_graph
from markush_map.graph.structure.site import FixSite, RingSite
from .utils.common import find_common_sets


@dataclass(frozen=True)
class ReactionStorage:
    reactions_smiles: List[str]
    p: List[float]

    @classmethod
    def from_uspto_csv(cls, path: str) -> 'ReactionStorage':
        df = pd.read_csv(path)
        if "rxn_Smiles" in df:
            reactions_smiles = list(df["rxn_Smiles"].values)
        else:
            reactions_smiles = list(df["rxnSmiles_Mapping_NameRxn"].values)
        p = [1 / len(s) ** 2 for s in reactions_smiles]
        sum_p = sum(p)
        p = [s / sum_p for s in p]
        return ReactionStorage(reactions_smiles, p)

    def choice(self) -> str:
        i = int(npr.choice(len(self.reactions_smiles), p=self.p))
        return self.reactions_smiles[i]


def load_uspto(path: str) -> List[str]:
    df = pd.read_csv(path)
    if "rxn_Smiles" in df:
        return list(df["rxn_Smiles"].values)
    return list(df["rxnSmiles_Mapping_NameRxn"].values)


@dataclass(frozen=True)
class MarkushEntity:
    smiles: str
    mol: Chem.Mol
    markush_graph: MarkushGraph
    idx_to_mn: Dict[int, int]
    idx_to_id: Dict[int, str]
    mn_to_id: Dict[int, str]

    @property
    def id_to_mn(self) -> Dict[str, int]:
        return {i: map_num for map_num, i in self.mn_to_id.items()}

    @property
    def site_available_map_num(self) -> List[int]:
        return [mn for mn, aid in self.mn_to_id.items()
                if self.markush_graph.mol_graph.get_atom(aid).properties.get("num_explicit_hs") > 0]

    @property
    def ring_atoms_map_num(self) -> List[Set[int]]:
        dict_ring_atoms = find_ring_atoms(self.smiles)
        return [{self.idx_to_mn[idx] for idx in atom_indices if idx in self.idx_to_mn.keys()}
                for atom_indices in dict_ring_atoms.values()]

    def get_self_atom_map(self) -> Dict[str, str]:
        return {atom_id: atom_id for atom_id in self.markush_graph.mol_graph.atoms.keys()}

    def get_atom_map_with_source(self, mn_to_id: Dict[int, str]) -> Dict[str, str]:
        atom_map = {}
        for mn, atom_id in mn_to_id.items():
            self_atom_id = self.mn_to_id.get(mn, None)
            if self_atom_id is not None:
                atom_map[atom_id] = self_atom_id
        return atom_map


def parse_rxn_smiles(rxn_smiles: str) -> Tuple[List[str], List[str]]:
    r_smiles, p_smiles = rxn_smiles.split(">>")
    return r_smiles.split("."), p_smiles.split(".")


def rxn_smiles_to_mols(rxn_smiles: str) -> Tuple[List[Chem.Mol], List[Chem.Mol]]:
    rs, ps = parse_rxn_smiles(rxn_smiles)
    return [Chem.MolFromSmiles(s) for s in rs], [Chem.MolFromSmiles(s) for s in ps]


def mol_to_markush_with_map(mol: Chem.Mol) -> Tuple[MarkushGraph, Dict[int, int], Dict[int, str], Dict[int, str]]:
    idx_to_mn = {a.GetIdx(): a.GetAtomMapNum() for a in mol.GetAtoms() if a.GetAtomMapNum()}
    mol_graph, idx_to_id = mol_to_graph(mol)
    markush_graph = MarkushGraph(mol_graph)
    return markush_graph, idx_to_mn, idx_to_id, {mn: idx_to_id[idx] for idx, mn in idx_to_mn.items()}


def smiles_to_entity(smiles: str) -> MarkushEntity:
    mol = Chem.MolFromSmiles(smiles)
    markush_graph, idx_to_mn, idx_to_id, nm_to_id = mol_to_markush_with_map(mol)
    return MarkushEntity(smiles, mol, markush_graph, idx_to_mn, idx_to_id, nm_to_id)


def rxn_smiles_to_entities(rxn_smiles: str) -> Tuple[List[MarkushEntity], List[MarkushEntity]]:
    rs, ps = parse_rxn_smiles(rxn_smiles)
    return [smiles_to_entity(s) for s in rs], [smiles_to_entity(s) for s in ps]


def add_fix_sites_in_markush_entities(
        markush_entities: List[MarkushEntity],
        dict_map_num2label: Dict[int, str],
):
    set_map_num = set(dict_map_num2label.keys())
    for entity in markush_entities:
        for map_num in set_map_num & set(entity.mn_to_id.keys()):
            label = dict_map_num2label[map_num]
            atom_id = entity.mn_to_id[map_num]
            entity.markush_graph.fix_sites.append(FixSite.from_case(label, "", [atom_id]))


def add_ring_sites_in_markush_entities(
        markush_entities: List[MarkushEntity],
        tuples_ring_label: List[Tuple[Set[int], str]],
):
    for entity in markush_entities:
        for ring, label in tuples_ring_label:
            atom_indices = [entity.mn_to_id.get(mn, None) for mn in ring]
            if None in atom_indices:
                continue
            entity.markush_graph.ring_sites.append(RingSite.from_case(label, "", atom_indices))


def build_single_port_fix_pair(markush_graph: MarkushGraph, label: str, smiles: str) -> Optional[FixPair]:
    fracture = MolFracture.from_smiles(smiles)
    sites = markush_graph.get_sites_with_label(label)
    for site in sites:
        if isinstance(site, FixSite):
            return FixPair([site], [fracture])
    return None


def build_ring_pair(markush_graph: MarkushGraph, atom_id: str, smiles: str) -> Optional[RingPair]:
    fracture = MolFracture.from_smiles(smiles, atom_id)
    sites = markush_graph.get_sites_with_atom_id(atom_id)
    for site in sites:
        if isinstance(site, RingSite):
            return RingPair([site], [fracture], [])
    return None


def build_ring_pairs(
        markush_graph: MarkushGraph,
        dict_atom2smiles: Dict[str, str],
        dict_atom2sub: Dict[str, str],
) -> List[RingPair]:
    atom_indices = set(dict_atom2smiles.keys()) | set(dict_atom2sub.keys())
    dict_aid_sid = markush_graph.get_dict_site_with_atom_indices(atom_indices, ring_site_only=True)
    dict_sid_fractures: Dict[str, List[MolFracture]] = {}
    dict_sid_atom_subs: Dict[str, List[AtomSub]] = {}
    for atom_id, smiles in dict_atom2smiles.items():
        fracture = MolFracture.from_smiles(smiles, atom_id)
        dict_sid_fractures.setdefault(dict_aid_sid[atom_id], []).append(fracture)
    for atom_id, sub_symbol in dict_atom2sub.items():
        atom_sub = AtomSub("", atom_id, markush_graph.mol_graph.get_atom(atom_id).symbol, sub_symbol)
        dict_sid_atom_subs.setdefault(dict_aid_sid[atom_id], []).append(atom_sub)
    site_indices = set(dict_sid_fractures.keys()) | set(dict_sid_atom_subs.keys())
    pairs = []
    for site_id in site_indices:
        site = markush_graph.get_site_with_id(site_id)
        if isinstance(site, RingSite):
            pairs.append(RingPair([site], dict_sid_fractures.get(site_id, []), dict_sid_atom_subs.get(site_id, [])))
    return pairs


def build_match_result(
        markush_graph: MarkushGraph,
        dict_label2smiles: Dict[str, str],
        dict_atom2smiles: Dict[str, str],
        dict_atom2sub: Dict[str, str],
) -> MatchResult:
    fix_pairs = [build_single_port_fix_pair(markush_graph, label, smiles) for label, smiles in dict_label2smiles.items()]
    fix_pairs = [p for p in fix_pairs if p is not None]
    ring_pairs = build_ring_pairs(markush_graph, dict_atom2smiles, dict_atom2sub)
    return MatchResult(fix_pairs, ring_pairs)


@dataclass(frozen=True)
class Abbrev:
    name: str
    smiles: str
    atoms: int
    symbol: str
    label: str
    label_w: str


@dataclass(frozen=True)
class AbbrevStorage:
    abbrevs: List[Abbrev]
    p: List[float]

    @classmethod
    def from_csv(cls, path: str) -> 'AbbrevStorage':
        df = pd.read_csv(path)
        abbrevs, p = [], []
        for _, row in df.iterrows():
            abbrevs.append(Abbrev(
                row["name"], row["smiles"], int(row["atoms"]), row["symbol"], row["label"], row["labelW"],
            ))
            p.append(1 / int(row["atoms"] ** 2))
        sum_p = sum(p)
        p = [s / sum_p for s in p]
        return AbbrevStorage(abbrevs, p)

    def choice(self) -> Abbrev:
        i = int(npr.choice(len(self.abbrevs), p=self.p))
        return self.abbrevs[i]


def get_valid_map_nums_and_pid(
        reactants_markush_entity: List[MarkushEntity],
        products_markush_entity: List[MarkushEntity],
) -> Tuple[Set[int], int]:
    r_map_num = [entity.site_available_map_num for entity in reactants_markush_entity]
    p_map_num = [entity.site_available_map_num for entity in products_markush_entity]
    p_map_cnt = [len(mn) for mn in p_map_num]
    pid = int(np.argmax(p_map_cnt))
    r_set_map_num = set(sum(r_map_num, []))
    p_set_map_num = set(p_map_num[pid])
    set_map_num = r_set_map_num & p_set_map_num
    return set_map_num, pid


def get_ring_map_nums(
        reactants_markush_entity: List[MarkushEntity],
        products_markush_entity: List[MarkushEntity],
) -> List[Set[int]]:
    r_ring_map_num = [entity.ring_atoms_map_num for entity in reactants_markush_entity]
    p_ring_map_num = [entity.ring_atoms_map_num for entity in products_markush_entity]
    r_rings, p_rings = sum(r_ring_map_num, []), sum(p_ring_map_num, [])
    return find_common_sets(r_rings, p_rings)


def sample_fix_sites(valid_map_nums: Set[int], sample_num: int, start_id: int = 1) -> Dict[int, str]:
    selected_map_nums: List[int] = list(npr.choice(list(valid_map_nums), sample_num, replace=True))
    return {mn: f"R[{i + start_id}]" for i, mn in enumerate(selected_map_nums)}


def sample_ring_sites(rings_map_nums: List[Set[int]], sample_num: int, start_id: int = 1) -> List[Tuple[Set[int], str]]:
    selected_rings: List[Set[int]] = list(npr.choice(list(rings_map_nums), sample_num, replace=False))
    return [(ring, f"R[{i + start_id}]") for i, ring in enumerate(selected_rings)]


def sample_flexible_site_map_nums(
        tuples_ring_label: List[Tuple[Set[int], str]],
        valid_map_nums: Set[int],
        atom_site_sample_num: int,
) -> List[int]:
    if len(tuples_ring_label):
        set_flexible_map_num = reduce(lambda x, y: x | y, [ring for ring, _ in tuples_ring_label])
    else:
        set_flexible_map_num = set()
    valid_site_map_nums = set_flexible_map_num & valid_map_nums
    return list(npr.choice(list(valid_site_map_nums), atom_site_sample_num, replace=False))


def sample_atom_sub_map_nums(
        tuples_ring_label: List[Tuple[Set[int], str]],
        valid_map_nums: Set[int],
        atom_sub_sample_num: int,
) -> List[int]:
    if len(tuples_ring_label):
        set_flexible_map_num = reduce(lambda x, y: x | y, [ring for ring, _ in tuples_ring_label])
    else:
        set_flexible_map_num = set()
    valid_sub_map_nums = set_flexible_map_num & valid_map_nums
    return list(npr.choice(list(valid_sub_map_nums), atom_sub_sample_num, replace=False))


COMMON_AGENTS = [
    "H2", "Pd/C", "H+", "OH-", "Br2", "Cl2", "Na", "K", "MnO2", "OsO4",
    "KMnO4", "NaBH4", "LiAlH4", "BBr3", "NaH", "PCC", "DIBAL-H", "TfOH", "TsOH", "NBS"
]

COMMON_SOLVENTS = [
    "H2O", "CH3OH", "C6H6", "CH2Cl2", "CHCl3", "C2H5OH", "THF", "DMF", "DMSO",
    "Et2O", "Acetone", "Toluene", "Hexane", "Diethyl",
    "Methanol", "Ethanol", "DCM", "Benzene",
]

ATOM_SUB_SYNBOLS = ["C", "N", "Si", "P"]


def sample_atom_sub_symbol() -> str:
    return random.choice(ATOM_SUB_SYNBOLS)


def process(
        reaction_smiles: str,
        abbrev_storage: AbbrevStorage,
        fix_sample_num: int = 1,
        ring_sample_num: int = 0,
        flexible_site_sample_num: int = 0,
        atom_sub_sample_num: int = 0,
) -> Tuple[str, str]:
    reactants_markush_entity, products_markush_entity = rxn_smiles_to_entities(reaction_smiles)
    valid_map_nums, pid = get_valid_map_nums_and_pid(reactants_markush_entity, products_markush_entity)
    rings_map_nums = get_ring_map_nums(reactants_markush_entity, products_markush_entity)
    dict_map_num2label = sample_fix_sites(valid_map_nums, fix_sample_num)
    tuples_ring_label = sample_ring_sites(rings_map_nums, ring_sample_num)

    add_fix_sites_in_markush_entities(reactants_markush_entity, dict_map_num2label)
    add_fix_sites_in_markush_entities(products_markush_entity, dict_map_num2label)
    add_ring_sites_in_markush_entities(reactants_markush_entity, tuples_ring_label)
    add_ring_sites_in_markush_entities(products_markush_entity, tuples_ring_label)

    main_entity = products_markush_entity[pid]
    flexible_site_map_nums = sample_flexible_site_map_nums(tuples_ring_label, valid_map_nums, flexible_site_sample_num)
    atom_sub_map_nums = sample_atom_sub_map_nums(tuples_ring_label, valid_map_nums, atom_sub_sample_num)

    dict_label2smiles = {l: abbrev_storage.choice().smiles for l in dict_map_num2label.values()}
    dict_mn2smiles = {mn: abbrev_storage.choice().smiles for mn in flexible_site_map_nums}
    dict_mn2sub = {mn: sample_atom_sub_symbol() for mn in atom_sub_map_nums}
    dict_atom2smiles = {main_entity.mn_to_id.get(mn): smiles for mn, smiles in dict_mn2smiles.items()
                        if mn in main_entity.mn_to_id.keys()}
    dict_atom2sub = {main_entity.mn_to_id.get(mn): sub_symbol for mn, sub_symbol in dict_mn2sub.items()
                        if mn in main_entity.mn_to_id.keys()}
    match_result = build_match_result(main_entity.markush_graph, dict_label2smiles, dict_atom2smiles, dict_atom2sub)

    result = main_entity.markush_graph.recover(
        match_result, main_entity.get_self_atom_map(), flexible_label_match=False)
    example = result.output_graph
    final_reactants_caption, final_products_caption = [], []
    for entity in reactants_markush_entity:
        result_ = entity.markush_graph.recover(
            match_result, entity.get_atom_map_with_source(main_entity.mn_to_id), flexible_label_match=False)
        final_reactants_caption.append(result_.output_graph.caption)
    for i, entity in enumerate(products_markush_entity):
        if i == pid:
            final_products_caption.append(example.caption)
        else:
            final_products_caption.append(entity.markush_graph.caption)

    reactants_caption = [entity.markush_graph.caption for entity in reactants_markush_entity]
    products_caption = [entity.markush_graph.caption for entity in products_markush_entity]
    return ".".join(reactants_caption) + ">>" + ".".join(products_caption) + "<<" + example.caption, \
        ".".join(final_reactants_caption) + ">>" + ".".join(final_products_caption)


def filter_entities(entities: List[MarkushEntity]) -> Tuple[List[MarkushEntity], List[MarkushEntity]]:
    n_entity = len(entities)
    captions = [e.markush_graph.caption for e in entities]
    site_nums = [len(e.markush_graph.get_all_sites()) for e in entities]
    sited_indices = []
    reserved_indices = []
    agent_indices = []
    for i in range(n_entity):
        if site_nums[i] > 0:
            sited_indices.append(i)
        elif len(captions[i]) >= 10:
            reserved_indices.append(i)
        elif len(captions[i]) >= 6:
            agent_indices.append(i)
    if len(reserved_indices) > 1:
        reserved_indices.sort(key=lambda x: len(captions[x]), reverse=True)
        agent_indices.extend(reserved_indices[1:])
        reserved_indices = reserved_indices[:1]
    if len(agent_indices) > 1:
        agent_indices.sort(key=lambda x: len(captions[x]), reverse=True)
        agent_indices = agent_indices[:2]
    return [entities[i] for i in (sited_indices + reserved_indices)], [entities[i] for i in agent_indices]


def extend_reactant(entity: MarkushEntity, label: Optional[str] = None) -> Optional[Dict[str, Union[str, List[str]]]]:
    try:
        sites = entity.markush_graph.get_all_sites()
        site_names = [s.label for s in sites]
        if label is None:
            label = str(random.randint(1, 10))
        descriptions = []
        for name in site_names:
            if random.random() < 0.5:
                name = name.replace("[", "").replace("]", "")
            agents = random.choices(COMMON_AGENTS, k=random.randint(1, 3))
            descriptions.append(f"{name} = " + ", ".join(agents))
        return {"smiles": entity.markush_graph.caption, "identifiers": [label], "descriptions": descriptions}
    except ValueError:
        return None


def extend_product(entity: MarkushEntity, label: Optional[str] = None) -> Optional[Dict[str, Union[str, List[str]]]]:
    try:
        if len(entity.markush_graph.get_all_sites()) == 0:
            return {"smiles": entity.markush_graph.caption, "identifiers": [], "descriptions": []}
        if label is None:
            label = str(random.randint(1, 10))
        descriptions = [f"{random.randint(1, 99)} examples"]
        if random.random() < 0.5:
            descriptions.append(f"{random.randint(1, 49)}-{random.randint(50, 99)}% yield")
        else:
            descriptions.append(f"up to {random.randint(70, 100)}% yield")
        if random.random() < 0.4:
            descriptions.append(f"dr up to {random.random() * 100:.1f}:{random.random() * 100:.1f}")
        return {"smiles": entity.markush_graph.caption, "identifiers": [label], "descriptions": descriptions}
    except ValueError:
        return None


def extend_example(
        graph: MarkushGraph,
        label: Optional[str] = None,
        reaction_str: Optional[str] = None,
        r_group_dict: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Union[str, List[str]]]]:
    try:
        if label is None:
            label = f"{random.choice(string.ascii_lowercase)}{random.randint(1, 10)}"
        return {
            "smiles": graph.caption,
            "identifiers": [label],
            "descriptions": [f"{random.randint(20, 99)}% yield"],
            "r_group_dict": r_group_dict or {},
            "reaction_str": reaction_str or "",
        }
    except ValueError:
        return None


def process_synfact(
        reaction_smiles: str,
        abbrev_storage: AbbrevStorage,
        example_sample_num: int = 4,
        fix_sample_num: int = 1,
        ring_sample_num: int = 1,
        flexible_site_sample_num: int = 1,
        atom_sub_sample_num: int = 1,
        save_reaction_str: bool = True,
) -> Dict:
    reactants_markush_entity, products_markush_entity = rxn_smiles_to_entities(reaction_smiles)
    valid_map_nums, pid = get_valid_map_nums_and_pid(reactants_markush_entity, products_markush_entity)
    rings_map_nums = get_ring_map_nums(reactants_markush_entity, products_markush_entity)
    dict_map_num2label = sample_fix_sites(valid_map_nums, fix_sample_num)
    tuples_ring_label = sample_ring_sites(rings_map_nums, ring_sample_num, start_id=fix_sample_num + 1)

    add_fix_sites_in_markush_entities(reactants_markush_entity, dict_map_num2label)
    add_fix_sites_in_markush_entities(products_markush_entity, dict_map_num2label)
    add_ring_sites_in_markush_entities(reactants_markush_entity, tuples_ring_label)
    add_ring_sites_in_markush_entities(products_markush_entity, tuples_ring_label)

    main_entity = products_markush_entity[pid]
    examples: List[MarkushGraph] = []
    final_reaction_strs: List[str] = []
    examples_r_group_dict: List[Dict[str, str]] = []
    for ei in range(example_sample_num):
        flexible_site_map_nums = sample_flexible_site_map_nums(
            tuples_ring_label, valid_map_nums, flexible_site_sample_num)
        atom_sub_map_nums = sample_atom_sub_map_nums(
            tuples_ring_label, valid_map_nums, atom_sub_sample_num)
        dict_label2abbrev: Dict[str, Abbrev] = {l: abbrev_storage.choice() for l in dict_map_num2label.values()}
        dict_label2smiles = {k: v.smiles for k, v in dict_label2abbrev.items()}
        dict_label2symbol = {k: v.symbol for k, v in dict_label2abbrev.items()}
        dict_mn2smiles = {mn: abbrev_storage.choice().smiles for mn in flexible_site_map_nums}
        dict_mn2sub = {mn: sample_atom_sub_symbol() for mn in atom_sub_map_nums}
        dict_atom2smiles = {main_entity.mn_to_id.get(mn): smiles for mn, smiles in dict_mn2smiles.items()
                            if mn in main_entity.mn_to_id.keys()}
        dict_atom2sub = {main_entity.mn_to_id.get(mn): sub_symbol for mn, sub_symbol in dict_mn2sub.items()
                         if mn in main_entity.mn_to_id.keys()}
        match_result = build_match_result(main_entity.markush_graph, dict_label2smiles, dict_atom2smiles, dict_atom2sub)
        result = main_entity.markush_graph.recover(
            match_result, main_entity.get_self_atom_map(), flexible_label_match=False)
        example = result.output_graph
        examples.append(example)
        examples_r_group_dict.append(dict_label2symbol)

        if save_reaction_str:
            final_reactants_caption, final_products_caption = [], []
            for entity in reactants_markush_entity:
                result_ = entity.markush_graph.recover(
                    match_result, entity.get_atom_map_with_source(main_entity.mn_to_id), flexible_label_match=False)
                final_reactants_caption.append(result_.output_graph.caption)
            for i, entity in enumerate(products_markush_entity):
                if i == pid:
                    final_products_caption.append(example.caption)
                else:
                    final_products_caption.append(entity.markush_graph.caption)
            final_reaction_strs.append(".".join(final_reactants_caption) + ">>" + ".".join(final_products_caption))

    reactants_filter_entity, agents_entity = filter_entities(reactants_markush_entity)
    products_filter_entity, _ = filter_entities(products_markush_entity)
    n_reactant, n_agent = len(reactants_filter_entity), len(agents_entity)
    letter = random.choice(string.ascii_lowercase)

    return {
        "generals": [{
            "reactants": [extend_reactant(
                e, f"{i + 1}"
            ) for i, e in enumerate(reactants_filter_entity)],
            "products": [extend_product(
                e, f"{n_reactant + n_agent + i + 1}"
            ) for i, e in enumerate(products_filter_entity)],
            "conditions": {
                "agents": [e.markush_graph.caption for e in agents_entity],
                "solvents": random.choices(COMMON_SOLVENTS, k=random.randint(1, 2)),
                "time": f"{random.randint(1, 10)}h",
                "temperature": f"{random.randint(-30, 200)}°C",
            }
        }],
        "examples": [ee for ee in [extend_example(
            e, f"{letter}{i + 1}",
            final_reaction_strs[i] if save_reaction_str else None,
            examples_r_group_dict[i],
        ) for i, e in enumerate(examples)] if ee is not None],
    }


if __name__ == '__main__':
    _r = load_uspto("/vepfs/fs_unifinder/yangsw/data/markush/gen/ci6b00564_si_002/dataSetA.csv")
    _s = _r[1]
    print(_s)

