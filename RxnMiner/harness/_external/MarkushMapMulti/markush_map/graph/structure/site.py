from dataclasses import dataclass, field
from typing import List, Optional, Dict

from .base import IdentifiedObject


@dataclass
class SitePort(IdentifiedObject):
    atom_id: str


@dataclass
class Site(IdentifiedObject):
    label: str
    num_copy: str
    ports: Dict[str, SitePort] = field(default_factory=dict)

    def get_ports_atom(self) -> List[str]:
        return [p.atom_id for p in self.ports.values()]


@dataclass
class FixSite(Site):
    @classmethod
    def from_case(cls, label: str, num_copy: str, atom_ids: List[str]) -> 'FixSite':
        site = FixSite("", label, num_copy)
        for i in atom_ids:
            port = SitePort("", i)
            site.ports[port.id] = port
        return site

    @property
    def only_port(self) -> Optional[SitePort]:
        if len(self.ports) != 1:
            return None
        return list(self.ports.values())[0]

    def __repr__(self) -> str:
        if self.num_copy:
            return f"FixSite({self.label}?{self.num_copy})"
        return f"FixSite({self.label})"

    def __str__(self) -> str:
        if self.num_copy:
            return f"{self.label}?{self.num_copy}"
        return f"{self.label}"

    def __eq__(self, other: 'FixSite') -> bool:
        return self.label == other.label

    def __lt__(self, other: 'FixSite') -> bool:
        if len(self.ports) == len(other.ports):
            return self.label < other.label
        return len(self.ports) < len(other.ports)


@dataclass
class RingSite(Site):
    @classmethod
    def from_case(cls, label: str, num_copy: str, atom_ids: List[str]) -> 'RingSite':
        site = RingSite("", label, num_copy)
        for i in atom_ids:
            port = SitePort("", i)
            site.ports[port.id] = port
        return site

    def __repr__(self) -> str:
        if self.num_copy:
            return f"RingSite({self.label}?{self.num_copy})"
        return f"RingSite({self.label})"

    def __str__(self) -> str:
        if self.num_copy:
            return f"{self.label}?{self.num_copy}"
        return f"{self.label}"
