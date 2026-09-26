import re
from typing import Dict, Tuple

SPECIFIC_GROUP_PATTERN = r"<a>(\d+):([a-zA-Z0-9\?\[\]]+)</a>"
RING_GROUP_PATTERN = r"<r>(\d+):([a-zA-Z0-9\?\[\]]+)</r>"


def parse_label_with_num_copy(label: str) -> Tuple[str, str]:
    if "?" in label:
        parts = label.split("?")
        if len(parts) != 2:
            raise ValueError(f"utils.parse_regex.parse_label_with_num_copy: cannot parse {label}")
        return parts[0], parts[1]
    return label, ""


def parse_markush_smiles(
        markush_smiles: str
) -> Tuple[str, Dict[int, Tuple[str, str]], Dict[int, Tuple[str, str]]]:
    if "<sep>" not in markush_smiles:
        return markush_smiles, {}, {}
    base_smiles, groups_str = markush_smiles.split("<sep>")

    def _find_groups(_pattern: str) -> Dict[int, Tuple[str, str]]:
        _matches = re.findall(_pattern, groups_str)
        return {int(_pos): parse_label_with_num_copy(_str) for _pos, _str in _matches}

    specific_groups = _find_groups(SPECIFIC_GROUP_PATTERN)
    ring_groups = _find_groups(RING_GROUP_PATTERN)
    return base_smiles, specific_groups, ring_groups


def standardize_smiles(smiles: str) -> str:
    smiles = smiles.replace("<sep>", "")
    return smiles


def get_smarts_query_str(smiles: str) -> str:
    smarts_query = []
    for char in smiles:
        if char == "*":
            smarts_query.append("[*,H]")  # 任意原子
        else:
            smarts_query.append(char)
    return "".join(smarts_query)


def index_map_num(s: str) -> Dict[int, int]:
    r_group_pattern = r":(\d+)\]"
    matches = re.findall(r_group_pattern, s)
    indices = [int(pos) for pos in matches]
    return {pos: i for i, pos in enumerate(indices)}
