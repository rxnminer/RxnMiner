import uuid
from dataclasses import dataclass


@dataclass
class IdentifiedObject:
    id: str

    def __post_init__(self):
        # 确保ID存在，如果没有提供则生成一个
        if not self.id:
            self.id = str(uuid.uuid4())

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.id == other.id

    def __hash__(self):
        return hash(self.id)
