from dataclasses import dataclass
from typing import List, Tuple, Dict, Set, Optional

from .structure.mol_fracture import MolFracture, AtomSub, get_fracture_ports_atom_dict
from .structure.site import FixSite, RingSite
from .utils.text import sort_and_group, pairing_dicts_values, check_lists_contain


@dataclass(frozen=True)
class FixPair:
    fix_sites: List[FixSite]
    fractures: List[MolFracture]

    def __repr__(self) -> str:
        return f"FixPair(fix_sites=[{', '.join([s.__str__() for s in self.fix_sites])}], " \
               f"fractures=[{', '.join([f.__str__() for f in self.fractures])}])"


@dataclass(frozen=True)
class RingPair:
    ring_sites: List[RingSite]
    fractures: List[MolFracture]
    atom_subs: List[AtomSub]

    def __repr__(self) -> str:
        return f"RingPair(ring_sites=[{', '.join([s.__str__() for s in self.ring_sites])}], " \
               f"fractures=[{', '.join([f.__str__() for f in self.fractures])}], " \
               f"atom_subs=[{', '.join([a.__str__() for a in self.atom_subs])}])"


@dataclass
class MatchResult:
    fix_pairs: List[FixPair]
    ring_pairs: List[RingPair]

    def __repr__(self) -> str:
        if len(self.fix_pairs) + len(self.ring_pairs) == 0:\
            return "MatchResult()"
        lt = "\t"
        ln = "\n"
        return f"MatchResult(\n" \
               f"{''.join([lt + p.__repr__() + ln for p in self.fix_pairs])}" \
               f"{''.join([lt + p.__repr__() + ln for p in self.ring_pairs])}" \
               f")"

    def get_paired_fix_sites(self) -> List[FixSite]:
        return sum([fix_pair.fix_sites for fix_pair in self.fix_pairs], [])

    def get_paired_ring_sites(self) -> List[RingSite]:
        return sum([ring_pair.ring_sites for ring_pair in self.ring_pairs], [])

    def get_paired_fractures(self) -> List[MolFracture]:
        fix_fractures = sum([fix_pair.fractures for fix_pair in self.fix_pairs], [])
        ring_fractures = sum([ring_pair.fractures for ring_pair in self.ring_pairs], [])
        return fix_fractures + ring_fractures

    def get_paired_atom_subs(self) -> List[AtomSub]:
        return sum([ring_pair.atom_subs for ring_pair in self.ring_pairs], [])

    def get_1to1or0_fix_pairs(self) -> List[FixPair]:
        one_fix_pairs = []
        for fix_pair in self.fix_pairs:
            if len(fix_pair.fix_sites) != 1 or len(fix_pair.fractures) > 1:
                continue
            if len(fix_pair.fix_sites[0].ports) != 1:
                continue
            if len(fix_pair.fractures) == 1 and len(fix_pair.fractures[0].outer_bonds) != 1:
                continue
            one_fix_pairs.append(fix_pair)
        return one_fix_pairs

    def get_dict_label_fracture(self) -> Dict[str, Optional[MolFracture]]:
        return {fix_pair.fix_sites[0].label:
                    fix_pair.fractures[0] if len(fix_pair.fractures) else None
                for fix_pair in self.get_1to1or0_fix_pairs()}

    def get_flexible_fractures(self) -> List[MolFracture]:
        fractures = []
        one_fix_pairs = self.get_1to1or0_fix_pairs()
        for fix_pair in self.fix_pairs:
            if fix_pair in one_fix_pairs:
                continue
            fractures.extend(fix_pair.fractures)
        for ring_pair in self.ring_pairs:
            fractures.extend(ring_pair.fractures)
        return fractures

    def get_atom_subs(self) -> List[AtomSub]:
        atom_subs = []
        for ring_pair in self.ring_pairs:
            atom_subs.extend(ring_pair.atom_subs)
        return atom_subs

    def has_flexible_fractures(self) -> bool:
        if len(self.ring_pairs):
            return True
        if len(self.get_1to1or0_fix_pairs()) < len(self.fix_pairs):
            return True
        return False


class SiteFractureMatcher:
    def __init__(
            self,
            fix_sites: List[FixSite],
            ring_sites: List[RingSite],
            fractures: List[MolFracture],
            atom_subs: List[AtomSub],
            verbose: bool = False,
    ):
        self.verbose = verbose
        self.dict_fix_site: Dict[str, FixSite] = {s.id: s for s in fix_sites}
        self.dict_ring_site: Dict[str, RingSite] = {s.id: s for s in ring_sites}
        self.dict_fracture: Dict[str, MolFracture] = {f.id: f for f in fractures}
        self.dict_atom_sub: Dict[str, AtomSub] = {a.id: a for a in atom_subs}
        self.dict_fix_site_ports_atom: Dict[str, List[str]] = {s.id: s.get_ports_atom() for s in fix_sites}
        self.dict_ring_site_ports_atom: Dict[str, List[str]] = {s.id: s.get_ports_atom() for s in ring_sites}
        self.dict_fracture_ports_atom: Dict[str, List[str]] = get_fracture_ports_atom_dict(fractures)

        self.left_fix_sids: Set[str] = set(self.dict_fix_site.keys())
        self.left_ring_sids: Set[str] = set(self.dict_ring_site.keys())
        self.left_fids: Set[str] = set(self.dict_fracture.keys())
        self.left_aids: Set[str] = set(self.dict_atom_sub.keys())
        self.match_result = MatchResult([], [])

    def get_left_fix_sites(self) -> List[FixSite]:
        return [self.dict_fix_site[sid] for sid in self.left_fix_sids]

    def get_left_ring_sites(self) -> List[RingSite]:
        return [self.dict_ring_site[sid] for sid in self.left_ring_sids]

    def get_left_fractures(self) -> List[MolFracture]:
        return [self.dict_fracture[fid] for fid in self.left_fids]

    def get_left_atom_subs(self) -> List[AtomSub]:
        return [self.dict_atom_sub[aid] for aid in self.left_aids]

    def add_fix_pair(self, sids: List[str], fids: List[str]):
        if self.verbose:
            print(f"\tpair fix_sites ({', '.join([self.dict_fix_site[sid].__repr__() for sid in sids])}) "
                  f"with fractures ({', '.join([self.dict_fracture[fid].__repr__() for fid in fids])})")
        for sid in sids:
            self.left_fix_sids.remove(sid)
        for fid in fids:
            self.left_fids.remove(fid)
        self.match_result.fix_pairs.append(FixPair(
            [self.dict_fix_site[sid] for sid in sids],
            [self.dict_fracture[fid] for fid in fids]
        ))

    def add_1to1_pairs(self, pairs_sid_fid: List[Tuple[str, str]]):
        for sid, fid in pairs_sid_fid:
            self.add_fix_pair([sid], [fid])

    def add_ring_pair(self, sids: List[str], fids: List[str], aids: List[str]):
        if self.verbose:
            print(f"\tpair ring_sites ({', '.join([self.dict_ring_site[sid].__repr__() for sid in sids])}) "
                  f"with fractures ({', '.join([self.dict_fracture[fid].__repr__() for fid in fids])}) "
                  f"and atom_subs ({', '.join([self.dict_atom_sub[aid].__repr__() for aid in aids])})")
        for sid in sids:
            self.left_ring_sids.remove(sid)
        for fid in fids:
            self.left_fids.remove(fid)
        for aid in aids:
            self.left_aids.remove(aid)
        self.match_result.ring_pairs.append(RingPair(
            [self.dict_ring_site[sid] for sid in sids],
            [self.dict_fracture[fid] for fid in fids],
            [self.dict_atom_sub[aid] for aid in aids],
        ))

    def _pair_fix_same_label_sites(self):
        # 处理同名site，这里假设每个site只有一个port，且和fracture都是一对一的
        for _sites in sort_and_group(self.get_left_fix_sites(), reverse=True, sort_by_size=True):
            if len(_sites) <= 1:
                continue

            _site_port_atom: Dict[str, str] = {}
            for _s in _sites:
                _ports_atom = self.dict_fix_site_ports_atom[_s.id]
                if len(_ports_atom) != 1:
                    raise ValueError(f"pair_fix_sites_fractures: site {_s.label} has {len(_ports_atom)} ports")
                _site_port_atom[_s.id] = _ports_atom[0]

            _final_pairs_sid_fid: Optional[List[Tuple[str, str]]] = None
            for _fractures in sort_and_group(self.get_left_fractures(), reverse=True, sort_by_size=True):
                if len(_fractures) < len(_sites):
                    continue
                _fracture_port_atom: Dict[str, str] = {}
                for _f in _fractures:
                    _ports_atom = self.dict_fracture_ports_atom[_f.id]
                    if len(_ports_atom) != 1:
                        continue
                    _fracture_port_atom[_f.id] = _ports_atom[0]
                _pairs_sid_fid = pairing_dicts_values(_site_port_atom, _fracture_port_atom)
                if len(_pairs_sid_fid) == len(_sites):
                    _final_pairs_sid_fid = _pairs_sid_fid

            # if _final_pairs_sid_fid is None:
            #     raise ValueError(f"_pair_fix_same_label_sites: site {_sites[0].label} cannot be paired")
            if _final_pairs_sid_fid is not None:
                self.add_1to1_pairs(_final_pairs_sid_fid)

    def _pair_fix_multi_port_fractures(self):
        # 处理有多个outer_bond（port）的fracture，优先匹配有多个port的site
        for _f in sorted(self.get_left_fractures(), reverse=True):
            _f_ports_atom = self.dict_fracture_ports_atom[_f.id]
            if len(_f_ports_atom) <= 1:
                continue
            _left_ports_atom = [_aid for _aid in _f_ports_atom]
            _sites: List[FixSite] = []
            for _s in sorted(self.get_left_fix_sites(), reverse=True):
                if len(_left_ports_atom) == 0:
                    break
                _s_ports_atom = self.dict_fix_site_ports_atom[_s.id]
                if len(_left_ports_atom) < len(_s_ports_atom):
                    continue
                if check_lists_contain(_left_ports_atom, _s_ports_atom):
                    for _aid in _s_ports_atom:
                        _left_ports_atom.remove(_aid)
                    _sites.append(_s)
            # if len(_left_ports_atom) > 0:
            #     raise ValueError(f"_pair_multi_port_fractures: fracture {_f.__repr__()} cannot be paired")
            if len(_left_ports_atom) == 0:
                self.add_fix_pair([_s.id for _s in _sites], [_f.id])

    def _pair_fix_1to1(self):
        # 处理只有一个outer_bond（port）的fracture，匹配同样只有一个port的site
        for _f in self.get_left_fractures():
            _f_ports_atom = self.dict_fracture_ports_atom[_f.id]
            if len(_f_ports_atom) != 1:
                continue
            _f_aid = _f_ports_atom[0]
            for _s in self.get_left_fix_sites():
                _s_ports_atom = self.dict_fix_site_ports_atom[_s.id]
                if len(_s_ports_atom) != 1:
                    continue
                _s_aid = _s_ports_atom[0]
                if _f_aid == _s_aid:
                    self.add_fix_pair([_s.id], [_f.id])
                    break
    def _pair_fix_1_h(self):
        # 对于未匹配的只有一个port的fix site，认为其取代H（即空取代）
        for _s in self.get_left_fix_sites():
            _s_ports_atom = self.dict_fix_site_ports_atom[_s.id]
            if len(_s_ports_atom) != 1:
                continue
            self.add_fix_pair([_s.id], [])

    def _pair_ring_unlimited(self):
        # 处理环取代
        for _s in self.get_left_ring_sites():
            _s_ports_atom = self.dict_ring_site_ports_atom[_s.id]
            _fractures: List[MolFracture] = []
            _atom_subs: List[AtomSub] = []
            # 环上取代
            for _f in self.get_left_fractures():
                _f_ports_atom = self.dict_fracture_ports_atom[_f.id]
                if check_lists_contain(_s_ports_atom, _f_ports_atom):
                    _fractures.append(_f)
            # 环原子取代
            for _a in self.get_left_atom_subs():
                if _a.atom_id in _s_ports_atom:
                    _atom_subs.append(_a)
            if len(_fractures) > 0 or len(_atom_subs) > 0:
                self.add_ring_pair([_s.id], [_f.id for _f in _fractures], [_a.id for _a in _atom_subs])
            else:
                self.add_ring_pair([_s.id], [], [])

    def match(self) -> 'SiteFractureMatcher':
        # 每组对应关系，考虑三种情况：
        # 1. 多对一：多个site共享一个fracture（如*C*匹配C1CC1）
        # 2. 一对多：一个site占有多个fracture（如C1*C1匹配CCCC）（暂时不考虑）
        # 3. 一对一：一个site对应一个fracture（如C1*C1匹配C1CCC1）
        # 除此之外，还需要并行讨论：
        # - 每个site拥有单个/多个port
        # - 同名site
        self._pair_fix_same_label_sites()
        self._pair_fix_multi_port_fractures()
        self._pair_fix_1to1()
        self._pair_fix_1_h()
        self._pair_ring_unlimited()
        return self

    def eval_result(self) -> float:
        if len(self.dict_fix_site):
            fix_site_score = len(self.match_result.get_paired_fix_sites()) / len(self.dict_fix_site)
        else:
            fix_site_score = 1.
        if len(self.dict_ring_site):
            ring_site_score = len(self.match_result.get_paired_ring_sites()) / len(self.dict_ring_site)
        else:
            ring_site_score = 1.
        if len(self.dict_fracture):
            fracture_score = len(self.match_result.get_paired_fractures()) / len(self.dict_fracture)
        else:
            fracture_score = 1.
        return (fix_site_score + ring_site_score + fracture_score) / 3
