from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple, Optional, Iterable

from rdkit import Chem

from .matcher import SiteFractureMatcher, MatchResult
from .structure.atom import Atom
from .structure.bond import Bond, BondType
from .structure.mol_fracture import MolFracture, AtomSub
from .structure.mol_graph import MolGraph
from .structure.process import graph_to_mol, graph_to_query, smiles_to_graph, graph_to_smiles
from .structure.site import SitePort, FixSite, RingSite, Site
from .utils.detect_ring import find_ring_atoms
from .utils.op_mol import get_mol_matches_with_query
from .utils.parse_regex import parse_markush_smiles


@dataclass(frozen=True)
class FitResult:
    score: float
    skeleton_graph: 'MarkushGraph'
    complete_graph: MolGraph
    skeleton_aid_to_complete_aid: Dict[str, str]
    match_result: MatchResult

    def __repr__(self) -> str:
        return f"FitResult(score={self.score}, " \
               f"skeleton_graph={self.skeleton_graph.caption}, " \
               f"complete_graph={graph_to_smiles(self.complete_graph)[0]}, " \
               f"match_result={self.match_result.__repr__()})"


@dataclass(frozen=True)
class RecoverResult:
    match_result: MatchResult
    input_graph: 'MarkushGraph'
    output_graph: 'MarkushGraph'

    def __repr__(self) -> str:
        return f"RecoverResult(match_result={self.match_result.__repr__()}, " \
               f"input_graph={self.input_graph.caption}, " \
               f"output_graph={self.output_graph.caption})"

    def __str__(self) -> str:
        return f"{self.input_graph.caption} -> {self.output_graph.caption}"


@dataclass
class MarkushGraph:
    mol_graph: MolGraph
    fix_sites: List[FixSite] = field(default_factory=list)
    ring_sites: List[RingSite] = field(default_factory=list)

    @classmethod
    def from_caption(cls, caption: str) -> Tuple['MarkushGraph', Dict[int, str]]:
        smiles, specific_groups, ring_groups = parse_markush_smiles(caption)
        dict_ring_atoms = find_ring_atoms(smiles)
        mol_graph, atom_idx_to_id = smiles_to_graph(smiles)

        fix_sites: List[FixSite] = []
        ring_sites: List[RingSite] = []
        # 处理固定取代基
        for atom_idx, (label, num_copy) in specific_groups.items():
            atom_id = atom_idx_to_id[atom_idx]
            attached_atoms = mol_graph.get_atom_neighbors(atom_id)
            fix_sites.append(FixSite.from_case(label, num_copy, [atom.id for atom in attached_atoms]))
        # 处理环取代基
        for ring_idx, (label, num_copy) in ring_groups.items():
            ring_atom_indices = dict_ring_atoms[ring_idx]
            ring_atoms = [mol_graph.get_atom(atom_idx_to_id[atom_idx]) for atom_idx in ring_atom_indices]
            ring_sites.append(RingSite.from_case(
                label, num_copy, [atom.id for atom in ring_atoms if mol_graph.check_atom_extra_hs(atom.id)]))
        # 删除通配符原子(*)
        for atom_id in [atom_idx_to_id[idx] for idx in specific_groups.keys()]:
            # neighbors = mol_graph.get_atom_neighbors(atom_id)
            # if len(neighbors) == 1:
            #     neighbor = neighbors[0]
            #     if neighbor.properties.get("aromatic", False):
            #         neighbor.properties["num_explicit_hs"] += 1
            mol_graph.remove_atom(atom_id)

        return MarkushGraph(mol_graph, fix_sites, ring_sites), atom_idx_to_id

    @property
    def caption(self) -> str:
        mol_graph, dict_site_atom_id = self.get_fix_star_graph()
        smiles, atom_map = graph_to_smiles(mol_graph)
        if Chem.MolFromSmiles(smiles) is None:
            raise ValueError(f"MarkushGraph.caption: Invalid SMILES {smiles}")
        dict_fix_label_atom_idx = {site.label: atom_map[dict_site_atom_id[site.id]] for site in self.fix_sites}
        dict_ring_label_ring_idx = {}
        if len(self.ring_sites) > 0:
            dict_ring_atoms = find_ring_atoms(smiles)
            for site in self.ring_sites:
                site_atom_indices = {atom_map[p.atom_id] for p in site.ports.values()}
                for ring_idx, ring_atom_indices in dict_ring_atoms.items():
                    if len(site_atom_indices) == len(site_atom_indices & ring_atom_indices):
                        dict_ring_label_ring_idx[site.label] = ring_idx
                        break
        attachment_info: List[str] = []
        for label, atom_idx in dict_fix_label_atom_idx.items():
            attachment_info.append(f"<a>{atom_idx}:{label}</a>")
        for label, ring_idx in dict_ring_label_ring_idx.items():
            attachment_info.append(f"<r>{ring_idx}:{label}</r>")
        if len(attachment_info) == 0:
            return smiles
        return f"{smiles}<sep>" + "".join(attachment_info)

    def get_fix_star_graph(self) -> Tuple[MolGraph, Dict[str, str]]:
        mol_graph = self.mol_graph.copy()
        dict_site_atom_id = {}
        # print(graph_to_smiles(mol_graph)[0])
        # for s in self.fix_sites:
        #     print(s.label, [p.atom_id for p in s.ports.values()])
        for site in self.fix_sites:
            atom = Atom("", "*")
            mol_graph.add_atom(atom)
            for port in site.ports.values():
                try:
                    mol_graph.add_bond(Bond("", atom.id, port.atom_id, BondType.SINGLE))
                except ValueError:
                    pass
                # port_atom = mol_graph.get_atom(port.atom_id)
                # if port_atom.properties.get("num_explicit_hs", 0) > 0:
                #     port_atom.properties["num_explicit_hs"] -= 1
            dict_site_atom_id[site.id] = atom.id
        # print(graph_to_smiles(mol_graph)[0])
        return mol_graph, dict_site_atom_id

    def get_ring_sub_graph(self) -> Tuple[MolGraph, Dict[str, str]]:
        mol_graph = self.mol_graph.copy()
        dict_atom_symbol = {}
        for site in self.ring_sites:
            for p in site.ports.values():
                atom = mol_graph.get_atom(p.atom_id)
                dict_atom_symbol[atom.id] = atom.symbol
                atom.symbol = "*"
        return mol_graph, dict_atom_symbol

    def get_all_sites(self) -> List[Site]:
        sites: List[Site] = []
        sites.extend(self.fix_sites)
        sites.extend(self.ring_sites)
        return sites

    def get_site_with_id(self, site_id: str) -> Optional[Site]:
        for site in self.get_all_sites():
            if site.id == site_id:
                return site
        return None

    def get_sites_with_label(self, label: str) -> List[Site]:
        return [site for site in self.get_all_sites() if site.label == label]

    def get_sites_with_atom_id(self, atom_id: str) -> List[Site]:
        return [site for site in self.get_all_sites() if atom_id in [port.atom_id for port in site.ports.values()]]

    def get_dict_site_with_atom_indices(self, atom_indices: Iterable[str], ring_site_only=False) -> Dict[str, str]:
        dict_aid_sid = {}
        for aid in atom_indices:
            sites = self.get_sites_with_atom_id(aid)
            if ring_site_only:
                sites = [s for s in sites if isinstance(s, RingSite)]
            if len(sites) > 0:
                dict_aid_sid[aid] = sites[-1].id
        return dict_aid_sid

    #     ports = [site.only_port for site in self.fix_sites]
    # def get_fix_ports(self) -> Dict[str, SitePort]:
    #     return {port.id: port for port in ports if port is not None}
    #
    # def get_flexible_ports(self) -> Dict[str, SitePort]:
    #     ports: List[SitePort] = []
    #     for site in self.fix_sites:
    #         if len(site.ports) > 1:
    #             ports.extend(site.ports.values())
    #     for site in self.ring_sites:
    #         ports.extend(site.ports.values())
    #     return {port.id: port for port in ports}

    def get_all_ports(self) -> Dict[str, SitePort]:
        ports: List[SitePort] = []
        for site in self.fix_sites:
            ports.extend(site.ports.values())
        for site in self.ring_sites:
            ports.extend(site.ports.values())
        return {port.id: port for port in ports}

    def fit_mol_graph(
            self,
            mol_graph: MolGraph,
            verbose: bool = False,
    ) -> Optional[FitResult]:
        # print(self.caption)
        # print(graph_to_smiles(self.mol_graph)[0])
        # print(graph_to_smiles(mol_graph)[0])
        skeleton_mol, s_atom_map = graph_to_mol(self.mol_graph)
        complete_mol, c_atom_map = graph_to_mol(mol_graph)
        c_atom_map_reverse: Dict[int, str] = {v: k for k, v in c_atom_map.items()}
        matches = get_mol_matches_with_query(complete_mol, skeleton_mol)
        if len(matches) == 0:
            ring_sub_graph, dict_atom_origin_symbol = self.get_ring_sub_graph()
            # print(graph_to_smiles(self.mol_graph)[0])
            # print(graph_to_smiles(ring_sub_graph)[0])
            skeleton_mol, s_atom_map = graph_to_query(ring_sub_graph)
            matches = get_mol_matches_with_query(complete_mol, skeleton_mol)
        else:
            dict_atom_origin_symbol = {}
        # print(dict_atom_origin_symbol)

        fit_results: List[FitResult] = []
        for mid, match in enumerate(matches):
            try:
                if verbose:
                    print(f"match ({mid}/{len(matches)}):", match)
                skeleton_aid_to_complete_aid: Dict[str, str] = {
                    s_atom_id: c_atom_map_reverse[match[s_atom_idx]]
                    for s_atom_id, s_atom_idx in s_atom_map.items()}
                dict_atom_final_symbol: Dict[str, str] = {
                    atom_id: mol_graph.get_atom(skeleton_aid_to_complete_aid[atom_id]).symbol
                    for atom_id, symbol in dict_atom_origin_symbol.items()}
                complete_aid_to_skeleton_aid: Dict[str, str] = {v: k for k, v in skeleton_aid_to_complete_aid.items()}
                matched_atom_ids = {c_atom_map_reverse[idx] for idx in match}
                branches = mol_graph.find_branches_masking_atoms(matched_atom_ids)
                mol_fractures = []
                for branch in branches:
                    bonds = mol_graph.find_bonds_between_branches(branch, matched_atom_ids)
                    mol_fractures.append(MolFracture.from_case(
                        mol_graph.get_branch_copy(branch), bonds, complete_aid_to_skeleton_aid))
                atom_subs = [
                    AtomSub("", atom_id, symbol, dict_atom_final_symbol[atom_id])
                    for atom_id, symbol in dict_atom_origin_symbol.items()
                    if symbol != dict_atom_final_symbol[atom_id]]
                # print(mol_fractures)
                # print(atom_subs)
                matcher = SiteFractureMatcher(
                    self.fix_sites, self.ring_sites, mol_fractures, atom_subs, verbose=verbose)
                matcher.match()
                score = matcher.eval_result()
                if verbose:
                    print(f"\tMatch score: {score}")
                fit_results.append(FitResult(
                    score, self, mol_graph, skeleton_aid_to_complete_aid, matcher.match_result,
                ))
                if score > 0.999:
                    break
            except ValueError as e:
                # raise e
                if verbose:
                    print(e)
        # for score, result in tuples_score_result:
        #     print(score)
        #     print(result)
        if len(fit_results) == 0:
            return None
        fit_results.sort(key=lambda x: x.score, reverse=True)
        return fit_results[0]

    def recover(
            self,
            match_result: MatchResult,
            atom_id_map: Optional[Dict[str, str]],
            flexible_label_match: bool = True,
    ) -> RecoverResult:
        """
        recover
        :param match_result: match result
        :param atom_id_map: fracture target atom id to self.mol_graph atom id
        :param flexible_label_match: e.g. match R[1] with R1
        :return:
        """
        dict_label_fracture = match_result.get_dict_label_fracture()
        mol_graph = self.mol_graph.copy()
        recovered_sites: Set[str] = set()

        for fix_site in self.fix_sites:
            if fix_site.label in dict_label_fracture.keys():
                fracture = dict_label_fracture[fix_site.label]
            elif flexible_label_match:
                temp_label1 = fix_site.label.replace("[", "").replace("]", "")
                temp_label2 = f"{fix_site.label[:1]}[{fix_site.label[1:]}]"
                if temp_label1 in dict_label_fracture.keys():
                    fracture = dict_label_fracture[temp_label1]
                elif temp_label2 in dict_label_fracture.keys():
                    fracture = dict_label_fracture[temp_label2]
                else:
                    continue
            else:
                continue
            if fracture is not None:
                if len(fix_site.ports) != 1 or len(fracture.outer_bonds) != 1:
                    raise ValueError(
                        f"MarkushGraph.recover: {fix_site.label} has {len(fix_site.ports)} site ports "
                        f"and {len(fracture.outer_bonds)} fracture outer-bonds")
                fracture = fracture.deepcopy()
                port, outer_bond = list(fix_site.ports.values())[0], fracture.outer_bonds[0]
                outer_bond.target = port.atom_id
                mol_graph.merge(fracture.graph).add_bond(outer_bond)
            recovered_sites.add(fix_site.id)

        if atom_id_map is not None:
            recovered_atoms: Set[str] = set()
            for fracture in match_result.get_flexible_fractures():
                ports_atoms = [atom_id_map.get(b.target, None) for b in fracture.outer_bonds]
                if None in ports_atoms:
                    continue
                fracture = fracture.deepcopy()
                mol_graph.merge(fracture.graph)
                for b in fracture.outer_bonds:
                    b.target = atom_id_map[b.target]
                    mol_graph.add_bond(b)
                    recovered_atoms.add(b.target)
            for atom_sub in match_result.get_atom_subs():
                atom_id = atom_id_map.get(atom_sub.atom_id, None)
                if atom_id is None:
                    continue
                mol_graph.get_atom(atom_id).change_symbol(atom_sub.sub_symbol)
                recovered_atoms.add(atom_id)
            for site in self.fix_sites:
                if site.id in recovered_sites:
                    continue
                ports_atom = set(site.get_ports_atom())
                if len(ports_atom) == len(ports_atom & recovered_atoms):
                    recovered_sites.add(site.id)
            for site in self.ring_sites:
                ports_atom = set(site.get_ports_atom())
                if len(ports_atom & recovered_atoms):
                    recovered_sites.add(site.id)
            for ring_pair in match_result.ring_pairs:
                if len(ring_pair.fractures) == 0 and len(ring_pair.atom_subs) == 0:
                    for rs in ring_pair.ring_sites:
                        for ss in self.get_sites_with_label(rs.label):
                            recovered_sites.add(ss.id)

        return RecoverResult(match_result, self, MarkushGraph(
            mol_graph,
            [s for s in self.fix_sites if s.id not in recovered_sites],
            [s for s in self.ring_sites if s.id not in recovered_sites],
        ))

    def remove_ring_sites(self):
        self.ring_sites.clear()
