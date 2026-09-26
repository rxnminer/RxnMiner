import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Set, Any, Iterator, Tuple

from .atom import Atom
from .bond import Bond


@dataclass
class MolGraph:
    """分子图类"""
    atoms: Dict[str, Atom] = field(default_factory=dict)  # 原子字典，键为原子ID
    bonds: Dict[str, Bond] = field(default_factory=dict)  # 键字典，键为键ID

    # 用于快速查找键的索引
    _atom_bonds: Dict[str, Set[str]] = field(default_factory=lambda: {})  # 原子ID到键ID的映射
    _source_bonds: Dict[str, Set[str]] = field(default_factory=lambda: {})  # 源原子ID到键ID的映射
    _target_bonds: Dict[str, Set[str]] = field(default_factory=lambda: {})  # 目标原子ID到键ID的映射

    def __post_init__(self):
        """初始化索引"""
        self._update_indices()

    def _update_indices(self) -> None:
        """更新索引结构"""
        self._atom_bonds = {}
        self._source_bonds = {}
        self._target_bonds = {}

        for atom_id in self.atoms:
            self._atom_bonds[atom_id] = set()
            self._source_bonds[atom_id] = set()
            self._target_bonds[atom_id] = set()

        for bond_id, bond in self.bonds.items():
            if bond.source in self.atoms and bond.target in self.atoms:
                self._atom_bonds.setdefault(bond.source, set()).add(bond_id)
                self._atom_bonds.setdefault(bond.target, set()).add(bond_id)
                self._source_bonds.setdefault(bond.source, set()).add(bond_id)
                self._target_bonds.setdefault(bond.target, set()).add(bond_id)

    @property
    def atom_bag(self) -> str:
        symbols = [a.symbol for a in self.get_all_atoms()]
        return "".join(sorted(symbols))

    # 原子操作
    def add_atom(self, atom: Atom) -> str:
        """添加原子，返回原子ID"""
        self.atoms[atom.id] = atom
        self._atom_bonds.setdefault(atom.id, set())
        self._source_bonds.setdefault(atom.id, set())
        self._target_bonds.setdefault(atom.id, set())
        return atom.id

    def get_atom(self, atom_id: str) -> Atom:
        """根据ID获取原子"""
        return self.atoms[atom_id]

    def get_atom_bonds(self, atom_id: str) -> List[Bond]:
        bond_ids = self._atom_bonds.get(atom_id, set())
        return [self.get_bond(i) for i in bond_ids]

    def get_atom_neighbors(self, atom_id: str) -> List[Atom]:
        atoms = []
        for bond in self.get_atom_bonds(atom_id):
            if bond.source == atom_id:
                atoms.append(self.get_atom(bond.target))
            elif bond.target == atom_id:
                atoms.append(self.get_atom(bond.source))
        return atoms

    def check_atom_extra_hs(self, atom_id: str) -> bool:
        atom = self.get_atom(atom_id)
        try:
            extra_hs = atom.get_property("num_explicit_hs") + atom.get_property("num_implicit_hs")
            return extra_hs > 0
        except ValueError:
            return False

    def remove_atom(self, atom_id: str) -> bool:
        """删除原子及与其相关的键，返回是否成功"""
        if atom_id not in self.atoms:
            return False

        # 先删除与此原子相关的键
        bonds_to_remove = list(self._atom_bonds.get(atom_id, set()))
        for bond_id in bonds_to_remove:
            self.remove_bond(bond_id)

        # 删除原子
        del self.atoms[atom_id]
        if atom_id in self._atom_bonds:
            del self._atom_bonds[atom_id]
        if atom_id in self._source_bonds:
            del self._source_bonds[atom_id]
        if atom_id in self._target_bonds:
            del self._target_bonds[atom_id]

        return True

    def find_atoms_by_property(self, key: str, value: Any) -> List[Atom]:
        """根据属性查找原子"""
        return [atom for atom in self.atoms.values() if atom.get_property(key) == value]

    # 键操作
    def add_bond(self, bond: Bond) -> str:
        """添加键，返回键ID"""
        if bond.source not in self.atoms or bond.target not in self.atoms:
            # print(bond.source in self.atoms)
            # print(bond.target in self.atoms)
            raise ValueError("MolGraph.add_bond: Source or target atom not found in graph")
        if bond.id in self.bonds.keys():
            raise ValueError("MolGraph.add_bond: duplicated bond id")

        self.bonds[bond.id] = bond

        self._atom_bonds.setdefault(bond.source, set()).add(bond.id)
        self._atom_bonds.setdefault(bond.target, set()).add(bond.id)
        self._source_bonds.setdefault(bond.source, set()).add(bond.id)
        self._target_bonds.setdefault(bond.target, set()).add(bond.id)

        return bond.id

    def get_bond(self, bond_id: str) -> Bond:
        """根据ID获取键"""
        return self.bonds[bond_id]

    def remove_bond(self, bond_id: str) -> bool:
        """删除键，返回是否成功"""
        if bond_id not in self.bonds:
            return False

        bond = self.bonds[bond_id]

        # 更新索引
        if bond.source in self._atom_bonds:
            self._atom_bonds[bond.source].discard(bond_id)
        if bond.target in self._atom_bonds:
            self._atom_bonds[bond.target].discard(bond_id)
        if bond.source in self._source_bonds:
            self._source_bonds[bond.source].discard(bond_id)
        if bond.target in self._target_bonds:
            self._target_bonds[bond.target].discard(bond_id)

        # 删除键
        del self.bonds[bond_id]

        return True

    def find_bonds_by_property(self, key: str, value: Any) -> List[Bond]:
        """根据属性查找键"""
        return [bond for bond in self.bonds.values() if bond.get_property(key) == value]

    def find_bonds_between(self, atom1_id: str, atom2_id: str) -> List[Bond]:
        """查找连接两个原子的所有键"""
        if atom1_id not in self.atoms or atom2_id not in self.atoms:
            return []

        bonds = []
        for bond_id in self._atom_bonds.get(atom1_id, set()):
            bond = self.bonds[bond_id]
            if (bond.source == atom1_id and bond.target == atom2_id) or \
                    (bond.source == atom2_id and bond.target == atom1_id):
                bonds.append(bond)

        return bonds

    def get_branch_copy(self, atom_ids: Set[str]) -> 'MolGraph':
        new_graph = MolGraph()
        for atom_id, atom in self.atoms.items():
            if atom_id in atom_ids:
                new_graph.add_atom(atom.copy())
        for bond_id, bond in self.bonds.items():
            if bond.source in atom_ids and bond.target in atom_ids:
                new_graph.add_bond(bond.copy())
        return new_graph

    def find_branch_of(self, atom_id: str, neglect_atoms: Set[str]) -> Set[str]:
        assert atom_id not in neglect_atoms
        visited = {atom_id}
        q = deque([atom_id])
        while len(q):
            i = q.pop()
            for atom in self.get_atom_neighbors(i):
                j = atom.id
                if j not in neglect_atoms and j not in visited:
                    q.append(j)
                    visited.add(j)
        return visited

    def find_branches_masking_atoms(self, atom_ids: Set[str]) -> List[Set[str]]:
        visited = atom_ids.copy()
        branches = []
        for atom_id in self.atoms.keys():
            if atom_id in visited:
                continue
            branch = self.find_branch_of(atom_id, atom_ids)
            visited |= branch
            branches.append(branch)
        return branches

    def find_bonds_between_branches(self, atom_ids_1: Set[str], atom_ids_2: Set[str]) -> List[Bond]:
        assert len(atom_ids_1 & atom_ids_2) == 0
        bonds = []
        for i in atom_ids_1:
            ns = {atom.id for atom in self.get_atom_neighbors(i)}
            for j in ns & atom_ids_2:
                bonds.extend(self.find_bonds_between(i, j))
        return bonds

    def merge(self, mol_graph: 'MolGraph') -> 'MolGraph':
        atom_keys, bond_keys = set(mol_graph.atoms.keys()), set(mol_graph.bonds.keys())
        if set(self.atoms.keys()) & atom_keys:
            raise KeyError("MolGraph.merge: duplicated atom keys")
        if set(self.bonds.keys()) & bond_keys:
            raise KeyError("MolGraph.merge: duplicated bond keys")
        for atom_id, atom in mol_graph.atoms.items():
            self.add_atom(atom)
        for bond_id, bond in mol_graph.bonds.items():
            self.add_bond(bond)
        return self

    def copy(self) -> 'MolGraph':
        """创建分子图的深复制"""
        new_graph = MolGraph()

        # 复制原子
        for atom_id, atom in self.atoms.items():
            new_graph.add_atom(atom.copy())

        # 复制键
        for bond_id, bond in self.bonds.items():
            new_graph.add_bond(bond.copy())

        return new_graph

    def deepcopy(self) -> Tuple['MolGraph', Dict[str, str]]:
        """
        Create a deep copy of the MolGraph object with new UUIDs for all atoms and bonds.
        Returns a new MolGraph instance with the same structure but different IDs.
        """
        # Create a new empty MolGraph
        new_graph = MolGraph()

        # Map old atom IDs to new atom IDs
        old_to_new_atom_id = {}

        # Copy all atoms with new IDs
        for old_atom_id, atom in self.atoms.items():
            # Create a copy of the atom
            new_atom = atom.copy()
            # Generate a new UUID for the atom
            new_atom_id = str(uuid.uuid4())
            # Update the atom's ID field
            new_atom.id = new_atom_id
            # Add the atom to the new graph
            new_graph.atoms[new_atom_id] = new_atom
            # Keep track of the ID mapping
            old_to_new_atom_id[old_atom_id] = new_atom_id

        # Copy all bonds with new IDs
        for old_bond_id, bond in self.bonds.items():
            # Create a copy of the bond
            new_bond = bond.copy()
            new_bond.source = old_to_new_atom_id[new_bond.source]
            new_bond.target = old_to_new_atom_id[new_bond.target]
            # Generate a new UUID for the bond
            new_bond_id = str(uuid.uuid4())
            # Update the bond's ID field
            new_bond.id = new_bond_id
            # Add the bond to the new graph
            new_graph.bonds[new_bond_id] = new_bond

            # Find which atoms this bond connects in the original graph
            # We need to update these references to use the new atom IDs
            for old_atom_id, bond_set in self._atom_bonds.items():
                if old_bond_id in bond_set:
                    new_atom_id = old_to_new_atom_id[old_atom_id]
                    if new_atom_id not in new_graph._atom_bonds:
                        new_graph._atom_bonds[new_atom_id] = set()
                    new_graph._atom_bonds[new_atom_id].add(new_bond_id)

            # Update source_bonds mapping
            for old_atom_id, bond_set in self._source_bonds.items():
                if old_bond_id in bond_set:
                    new_atom_id = old_to_new_atom_id[old_atom_id]
                    if new_atom_id not in new_graph._source_bonds:
                        new_graph._source_bonds[new_atom_id] = set()
                    new_graph._source_bonds[new_atom_id].add(new_bond_id)

            # Update target_bonds mapping
            for old_atom_id, bond_set in self._target_bonds.items():
                if old_bond_id in bond_set:
                    new_atom_id = old_to_new_atom_id[old_atom_id]
                    if new_atom_id not in new_graph._target_bonds:
                        new_graph._target_bonds[new_atom_id] = set()
                    new_graph._target_bonds[new_atom_id].add(new_bond_id)

        return new_graph, old_to_new_atom_id

    def get_all_atoms(self) -> Iterator[Atom]:
        """获取所有原子的迭代器"""
        return iter(self.atoms.values())

    def get_all_bonds(self) -> Iterator[Bond]:
        """获取所有键的迭代器"""
        return iter(self.bonds.values())

    def get_atom_count(self) -> int:
        """获取原子数量"""
        return len(self.atoms)

    def get_bond_count(self) -> int:
        """获取键数量"""
        return len(self.bonds)
