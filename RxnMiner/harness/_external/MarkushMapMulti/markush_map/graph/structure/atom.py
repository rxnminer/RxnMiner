import copy
from dataclasses import dataclass, field
from typing import Dict, Any

from .base import IdentifiedObject

DICT_SYMBOL_VALENCE = {
    "C": 4,
    "N": 3,
    "Si": 4,
    "P": 5,
}


@dataclass
class Atom(IdentifiedObject):
    """原子类"""
    symbol: str
    properties: Dict[str, Any] = field(default_factory=dict)  # 属性字典，例如元素类型、电荷等

    def get_property(self, key: str) -> Any:
        """获取属性"""
        return self.properties.get(key)

    def set_property(self, key: str, value: Any) -> None:
        """设置属性"""
        self.properties[key] = value

    def has_property(self, key: str) -> bool:
        """检查是否有某属性"""
        return key in self.properties

    def change_symbol(self, new_symbol: str):
        symbol = self.symbol
        if symbol == new_symbol:
            return
        if symbol not in DICT_SYMBOL_VALENCE.keys():
            raise KeyError(f"Atom.change_symbol: {symbol} not in DICT_SYMBOL_VALENCE.keys()")
        v, nv = DICT_SYMBOL_VALENCE[symbol], DICT_SYMBOL_VALENCE[new_symbol]
        self.symbol = new_symbol
        self.properties["num_explicit_hs"] += nv - v

    def copy(self) -> 'Atom':
        """创建原子的深复制"""
        return Atom(id=self.id, symbol=self.symbol, properties=copy.deepcopy(self.properties))
