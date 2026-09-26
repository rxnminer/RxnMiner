import json
import re
from dataclasses import dataclass
from functools import reduce
from pathlib import Path
from typing import List, Dict, Set, Optional

import pandas as pd
from rdkit import Chem, RDLogger

RDLogger.DisableLog('rdApp.*')

_abbrev_df = pd.read_csv(
    str(Path(__file__).parent.parent / 'Translator/utils/abbrevs.csv'), header=0, index_col=False
)
ABBREV_SYNBOLS = set(_abbrev_df["symbol"])
SPECIAL_LABELS = {"Ar", }
FALSE_LABELS = {"rr", "dr", }


@dataclass(frozen=True)
class RGroupParseResult:
    dict_r_group: Dict[str, List[str]]
    relations: List[str]

    @classmethod
    def merge(cls, results: List['RGroupParseResult']) -> 'RGroupParseResult':
        dict_r_group = _merge_dicts([r.dict_r_group for r in results])
        relations = sum([r.relations for r in results], [])
        return RGroupParseResult(dict_r_group, relations)


def polish_abbrev(s: str) -> List[str]:
    if len(s) == 0:
        return []
    if s in ABBREV_SYNBOLS:
        return [s]
    if s.endswith(","):
        return polish_abbrev(s[:-1])
    if s.isdigit():
        # return []
        raise ValueError(f"polish_abbrev false string: {s}")
    if re.fullmatch(r"\d[A-Za-z]", s):
        raise ValueError(f"polish_abbrev false string: {s}")
    if re.fullmatch(r"\d+(\.\d+)?:\d+(\.\d+)?", s):
        raise ValueError(f"polish_abbrev false string: {s}")
    try:
        Chem.MolFromSmiles(s)
        return [s]
    except:
        pass
    raise ValueError(f"polish_abbrev new string: {s}")


def polish_abbrevs(strings: List[str]) -> List[str]:
    new_strings = []
    for s in strings:
        new_strings.extend(polish_abbrev(s))
    return new_strings


def _check_label(text: str) -> bool:
    if "%" in text:
        return False
    if re.fullmatch(r"R\d?", text):
        return True
    if text == "X":
        return True
    if re.fullmatch(r"Ar\d?", text):
        return True
    if text in SPECIAL_LABELS:
        return True
    if text in FALSE_LABELS:
        return False
    raise ValueError(f"_check_label cannot recognize: {text}")


def parse_left(text: str) -> List[str]:
    texts = [t.strip() for t in re.split(r"[,/]", text)]
    texts = [t for t in texts if _check_label(t)]
    return texts


def _split_abbrevs_with_comma_robust(text: str) -> List[str]:
    texts = text.split(",")
    new_texts = []
    for t in texts[::-1]:
        t = t.strip()
        if len(new_texts) == 0 or not t.isdigit():
            new_texts.append(t)
        elif new_texts[-1][0].isdigit():
            new_texts[-1] = f"{t},{new_texts[-1]}"
        else:
            new_texts.append(t)
    return new_texts[::-1]


def parse_right(text: str) -> List[str]:
    texts = [t.strip() for t in re.split(r"/|and|or", text)]
    texts = sum([_split_abbrevs_with_comma_robust(t) for t in texts], [])
    texts = polish_abbrevs(texts)
    return texts


def parse_equation(left: str, right: str) -> Dict[str, List[str]]:
    lefts = parse_left(left)
    rights = parse_right(right)
    if len(lefts) == 0 or len(rights) == 0:
        raise ValueError(f"parse_equation no lefts or rights: {left}={right}")
    return {t: rights for t in lefts}


def _check_equation(text: str) -> bool:
    return len(text.replace("=", "")) == len(text) - 1


def _check_all_equation(texts: List[str]) -> bool:
    return all([_check_equation(t) for t in texts])


def _merge_dicts(ds: List[Dict[str, List[str]]]) -> Dict[str, List[str]]:
    new_dict: Dict[str, Set[str]] = {}
    for d in ds:
        for k, vs in d.items():
            new_dict.setdefault(k, set()).update(vs)
    return {k: list(vs) for k, vs in new_dict.items()}


def _try_split_with_comma_robust(text: str) -> Optional[List[str]]:
    comma_texts = text.split(",")
    new_texts = []
    for t in comma_texts:
        if len(new_texts) == 0 or _check_equation(t):
            new_texts.append(t)
        else:
            new_texts[-1] += f",{t}"
    return new_texts


def _split_equations(text: str) -> List[str]:
    valid_texts_list = [texts for texts in [
        text.split(";"), text.split(" "), text.split(","),
    ] if _check_all_equation(texts)]
    if len(valid_texts_list) > 0:
        return valid_texts_list[0]

    texts = _try_split_with_comma_robust(text)
    if texts is not None:
        return texts

    raise ValueError(f"_split_equations cannot parse: {text}")


def parse_equation_multi(text: str) -> Dict[str, List[str]]:
    texts = _split_equations(text)
    ds = [parse_equation_full(t, recursively_called=t == text) for t in texts]
    return _merge_dicts(ds)


def _process_equation_with_multiple_equal_robust(groups: List[str]) -> Dict[str, List[str]]:
    assert len(groups) > 2
    label_cnt = 0
    for g in groups:
        if _check_label(g):
            label_cnt += 1
        else:
            break
    right = "=".join(groups[label_cnt:])
    ds = [parse_equation(groups[i], right) for i in range(label_cnt)]
    return _merge_dicts(ds)


def parse_equation_full(text: str, panic=True, recursively_called=False) -> Optional[Dict[str, List[str]]]:
    groups = text.split("=")
    try:
        if len(groups) == 2:
            return parse_equation(groups[0], groups[1])
        elif len(groups) > 2:
            if recursively_called:
                return _process_equation_with_multiple_equal_robust(groups)
            return parse_equation_multi(text)
        raise ValueError(f"parse_equation_full cannot parse: {text}")
    except Exception as e:
        if panic:
             raise e
        # print(e)
    return None


def parse_pattern_multiline_yield(texts: List[str]) -> List[RGroupParseResult]:
    results = []
    for t in texts:
        groups = re.split("[:：]", t)
        if len(groups) < 2:
            raise ValueError(f"_parse_multiline_yield groups != 2: {t}")
        equation, relation = groups[0], ":".join(groups[1:])
        results.append(RGroupParseResult(parse_equation_full(equation), [relation]))
    return results


def _merge_molecule_with_comma(groups: List[str]) -> List[str]:
    new_groups = []
    status = 0  # 0: normal; 1: linkable

    def _update_status(_g: str) -> bool:
        nonlocal status
        if status == 1:
            if _g.isdigit():
                return True
            if _g[0].isdigit():
                status = 0
                return True
        _groups = _g.split("=")
        if len(_groups) > 1 and _groups[-1].isdigit():
            status = 1
            return False
        status = 0
        return False

    for g in groups:
        will_link = _update_status(g)
        if will_link:
            new_groups[-1] += f",{g}"
        else:
            new_groups.append(g)
    return new_groups


def parse_pattern_multiline_yield_no_colon(texts: List[str]) -> List[RGroupParseResult]:
    results = []
    for t in texts:
        groups = t.split(",")
        groups = _merge_molecule_with_comma(groups)
        if len(groups) > 1:
            equations = [parse_equation_full(g, panic=False) for g in groups]
            equation_ids = set([i for i, e in enumerate(equations) if e is not None])
            results.append(RGroupParseResult(
                _merge_dicts([equations[i] for i in equation_ids]),
                [groups[i] for i in range(len(groups)) if i not in equation_ids],
            ))
        else:
            equal_groups = t.split("=")
            if len(equal_groups) > 2:
                results.append(RGroupParseResult(parse_equation_full("=".join(equal_groups[:-1])), [equal_groups[-1]]))
            else:
                raise ValueError(f"parse_pattern_multiline_yield_no_colon: {t}")
    return results


def parse_r_group_nexus(texts: List[str], recursively_called=False) -> List[RGroupParseResult]:
    default_result = [RGroupParseResult({}, texts)]
    n_text = len(texts)
    if n_text == 0:
        return default_result
    equal_text_ids = [i for i, t in enumerate(texts) if "=" in t]
    colon_text_ids = [i for i, t in enumerate(texts) if ":" in t or "：" in t]
    n_equal_text = len(equal_text_ids)
    n_colon_text = len(colon_text_ids)
    if n_text == n_equal_text == n_colon_text:
        try:
            return parse_pattern_multiline_yield(texts)
        except ValueError as e:
            # print(e)
            pass
    if n_text == n_equal_text:
        try:
            return parse_pattern_multiline_yield_no_colon(texts)
        except ValueError as e:
            # print(e)
            pass
    equation_dicts = [parse_equation_full(t, panic=False) for t in texts]
    equation_ids = [i for i, d in enumerate(equation_dicts) if d is not None]
    if len(equation_ids) == 1:
        equation_id = equation_ids[0]
        relation_texts = [texts[i] for i in range(n_text) if i != equation_id]
        return [RGroupParseResult(equation_dicts[equation_id], relation_texts)]
    if len(texts) == 6:
        if "from" in texts[0] and "from" in texts[3] and "yield" in texts[1] and "yield" in texts[4]:
            return [RGroupParseResult({}, texts[:3]), RGroupParseResult({}, texts[3:])]
    if len(equation_ids) == 0:
        return [RGroupParseResult({}, texts)]
    if n_text == n_equal_text == n_colon_text:
        return [RGroupParseResult({}, [text]) for text in texts]

    if not recursively_called:
        results_list = [parse_r_group_nexus([text], recursively_called=True) for text in texts]
        results = sum(results_list, [])
        no_key_results, key_results = [], []
        for r in results:
            if len(r.dict_r_group):
                key_results.append(r)
            else:
                no_key_results.append(r)
        keys_list = [set(r.dict_r_group.keys()) for r in key_results]
        if len(keys_list) > 0:
            keys_all = reduce(lambda x, y: x | y, keys_list)
            keys_cnt = [len(keys) for keys in keys_list]
            keys_max_cnt = max(keys_cnt)
            keys_sum = sum(keys_cnt)
            if len(keys_all) == keys_sum:  # one sample
                return [RGroupParseResult.merge(results)]
            elif len(keys_all) == keys_max_cnt:  # multiple samples
                return [RGroupParseResult.merge([r] + no_key_results) for r in key_results]
    raise ValueError(f"parse_r_group_nexus cannot parse: {texts}")


if __name__ == '__main__':
    # _cases = [
    #     ["X=CH2:99% NMR yield", "X=O:74% NMR yield", "X=SO2:59%yield", "X=NBz:74% yield"],
    #     ["92% yield, 96% ee", "Ar=4-BrC6H4"],
    #     ["74% yield,95% ee dr = 14:1"],
    #     ["R1=Ph, R2=Ph,4c:49% yield", "R1=4-MeC6H4,R2=Ph,4d:62% yield", "R1=4-MeC6H4,R2=4-MeOC6H4, 4e:45% yield",
    #      "R1=4-MeC6H4,R2=4-CNC6H4, 4f:23% yield", "R1=Ph,R2=4-MeSO2C6H4, 4g:32% yield",
    #      "R1=Ph,R2=4-MeOC6H4, 4h:45% yield", "R1=4-CIC6H4,R2=Ph, 4I:52% yield", "R1=4CIC6H4,R2=4CNC6H4, 4j:39% yield",
    #      "R1=4-CIC6H4,R2=4-MeOC6H4, 4k:68% yield", "R1=4-CIC6H4,R2=3,5-(MeO)2C6H3, 4I:60% yield"],
    # ]
    # print(len(ABBREV_SYNBOLS))
    with open("tmp/250619-eg.jsonl", encoding="utf-8") as fp:
        _lines = fp.readlines()
    _bad_cases = {225, 231, 232, 233, 234, 235, 236}
    _slice = slice(644, 645, 1)
    # _slice = slice(839, 840, 1)
    for _i in range(_slice.start, _slice.stop, _slice.step):
        if _i in _bad_cases:
            continue
        _line = _lines[_i]
        _d = json.loads(_line)
        _text, _relations = _d["text"], _d["relations"]
        print(f"\nCASE {_i}: {_relations}")
        try:
            _results = parse_r_group_nexus(_relations)
            for _r in _results:
                print(f"\t{_r}")
        except ValueError as _e:
            raise _e
            # print(_e)
