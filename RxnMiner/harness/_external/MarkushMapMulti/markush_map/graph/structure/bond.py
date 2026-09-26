import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, Union, Set, Tuple

from .base import IdentifiedObject


class BondType(Enum):
    SINGLE = "single"
    DOUBLE = "double"
    TRIPLE = "triple"
    AROMATIC = "aromatic"
    UNKNOWN = "unknown"


@dataclass
class Bond(IdentifiedObject):
    """化学键类"""
    source: str  # 源原子ID
    target: str  # 目标原子ID
    type: BondType
    properties: Dict[str, Any] = field(default_factory=dict)  # 属性字典，例如键类型、键序等

    def get_source_target(self, atom_ids: Union[str, Set[str]]) -> Tuple[str, str]:
        if isinstance(atom_ids, str):
            atom_ids = {atom_ids}
        if self.source in atom_ids:
            return self.source, self.target
        elif self.target in atom_ids:
            return self.target, self.source
        raise KeyError(f"Bond.get_source_target: {self.source} & {self.target} not in {atom_ids}")

    def get_property(self, key: str) -> Any:
        """获取属性"""
        return self.properties.get(key)

    def set_property(self, key: str, value: Any) -> None:
        """设置属性"""
        self.properties[key] = value

    def has_property(self, key: str) -> bool:
        """检查是否有某属性"""
        return key in self.properties

    def copy(self) -> 'Bond':
        """创建键的深复制"""
        return Bond(
            id=self.id,
            source=self.source,
            target=self.target,
            type=self.type,
            properties=copy.deepcopy(self.properties)
        )
