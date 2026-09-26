import re
from typing import List, Dict, Set, Tuple


def polish_abbrevs(strings: List[str]) -> List[str]:
    return strings


def parse_r_group_dict(text: str) -> Tuple[Dict[str, List[str]], str]:
    if ";" in text:
        d, useless_strings = parse_r_group_dict_multiple(text.split(";"))
        return d, ";".join(useless_strings)
    groups = text.split("=")
    if len(groups) != 2:
        if len(groups) > 2:
            space_groups = text.split(" ")
            if len(space_groups) == len(groups) - 1:
                d, useless_strings = parse_r_group_dict_multiple(space_groups)
                return d, ";".join(useless_strings)
        return {}, text
    left, right = groups[0], groups[1]
    lefts = re.split(r"[,/]", left)
    rights = re.split(r'[,/]|and|or', right)
    d = {}
    for x in lefts:
        d[x.strip()] = polish_abbrevs([y.strip() for y in rights])
    if len(d) == 0:
        return {}, text
    return d, ""


def parse_r_group_dict_multiple(texts: List[str]) -> Tuple[Dict[str, List[str]], List[str]]:
    d: Dict[str, Set[str]] = {}
    useless_strings: List[str] = []
    for t in texts:
        dd, useless_string = parse_r_group_dict(t)
        if useless_string != "":
            useless_strings.append(useless_string)
        for k, vs in dd.items():
            d.setdefault(k, set()).update(set(vs))
    return {k: list(vs) for k, vs in d.items()}, useless_strings


if __name__ == '__main__':
    _cases = [
        ["R1= Me, CF3, F, CN, OMe, CO2Me, COPh, NO2 ", "R2=H, Et"],
        ["n = 1-3", "R1/R2 = H, Me"],
        ["(1.0 equiv)", "R1,R2=Alk,Bn;R3=Ar,HetAr"],
        ["(1 equiv)", "R = (substituted) aromatics, heteroaromatics, (substituted) aliphatics, styryls"],
        ["E or Z,R1=Alk,Ar", " R\u00b2=H,Alk"],
        ["2.0 equiv", " X = O, Si, B and C"],
        ["R=H or Me"],
        ["R1=ArAlk R\u00b2=Me,Et"],
        ["1.0 equiv", "R4=H,Me R5=H,Ph"],
    ]
    for _case in _cases:
        print(parse_r_group_dict_multiple(_case))
