import json
import logging
import os
import sys
import warnings
from typing import Dict, List, Any, Optional

import pandas as pd

logging.getLogger("torch").setLevel(logging.ERROR)
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# from markush_map.data.reaction import Synfact, Reaction, SYNFACT_TYPE, COMPONENT_TYPE, REACTION_TYPE
from markush_map.graph.reaction import Synfact, SYNFACT_TYPE, COMPONENT_TYPE, REACTION_TYPE
from graph_match.Translator import Translator

refactor = Translator()


def process(d: SYNFACT_TYPE, name: str = "null", verbose=False, debug=False) -> Dict[str, Any]:
    for reaction in d["general_reactions"]:
        reaction: REACTION_TYPE
        if debug:
            print(reaction)
        for obj in reaction:
            # if obj["type"] == "products":
            #     print(obj)
            if obj["type"] == "products" or obj["type"] == "reactants":
            # if obj["type"] == "reactants":
                new_caption = refactor.refactor(obj["text"]).caption
                # print(obj["text"], new_caption)
                obj["text"] = new_caption
    for example in d["examples"]:
        example: COMPONENT_TYPE
        if debug:
            print(example)
        new_caption = refactor.refactor(example["text"]).caption
        example["text"] = new_caption
    synfact = Synfact.from_json(d)
    # print(synfact.reactions[0].products)
    dict_reaction_fit_result = synfact.fit_reactions_with_examples(use_tqdm=True, debug=debug)
    total_num_case = len(synfact.reactions) * len(synfact.examples)
    fetched_num_case = 0
    passed_num_case = 0
    for (rid, eid), reaction_fit_result in dict_reaction_fit_result.items():
        output_reaction = reaction_fit_result.output_reaction
        if verbose:
            print(f"FOR REACTION #{rid} & EXAMPLE #{eid}:")
            print(reaction_fit_result.__repr__())
        reactant_smiles = ".".join([c.smiles for c in output_reaction.reactants])
        products_smiles = ".".join([c.smiles for c in output_reaction.products])
        fetched_num_case += 1
        if "*" not in reactant_smiles and "*" not in products_smiles:
            passed_num_case += 1
    print(f"\tFor case {name}: {passed_num_case} / {fetched_num_case} / {total_num_case}")
    return {
        "pass": passed_num_case,
        "fetch": fetched_num_case,
        "total": total_num_case,
    }


def display_result(result: Dict[str, List]):
    print(f"PASSED: {int(sum([p == t for p, t in zip(result['pass'], result['total'])]))} / {len(result['name'])}")
    print(f"AVERAGE FETCH RATIO: {sum(result['fetch']) / sum(result['total'])}")
    print(f"AVERAGE PASS RATIO: {sum(result['pass']) / sum(result['total'])}")



def main(src_dir: str, input_result_path: str, output_result_path: str, ref_result_path: Optional[str] = None):
    if ref_result_path is not None:
        ref_result: Dict[str, List] = pd.read_csv(ref_result_path).to_dict(orient="list")
        ref_names = set(ref_result["name"])
    else:
        ref_names = None

    json_paths = []
    for file in os.listdir(src_dir):
        if not file.endswith("json"):
            continue
        if ref_names is not None and file not in ref_names:
            continue
        json_paths.append(os.path.join(src_dir, file))
    n_case = len(json_paths)
    if os.path.isfile(input_result_path):
        result: Dict[str, List] = pd.read_csv(input_result_path).to_dict(orient="list")
    else:
        result: Dict[str, List] = {"name": [], "pass": [], "fetch": [], "total": []}

    if input_result_path != output_result_path:
        passed_indices = []
        for i in range(len(result["name"])):
            if result["pass"][i] == result["total"][i]:
                passed_indices.append(i)
        result = {k: [v[i] for i in passed_indices] for k, v in result.items()}
    processed_names = set(result["name"])

    for i, json_path in enumerate(json_paths):
        name = os.path.basename(json_path)
        if name in processed_names:
            print(f"{i}/{n_case}: {name} already processed")
            continue
        print(f"{i}/{n_case}: {name}")
        with open(json_path) as fp:
            try:
                info_dict = process(json.load(fp), name)
            except Exception as e:
                print(f"{json_path}: {e.__class__.__name__}(e)")
                info_dict = None
        if info_dict is None:
            continue
        info_dict["name"] = name
        for k, v in info_dict.items():
            if k in result.keys():
                result[k].append(v)
        pd.DataFrame(result).to_csv(output_result_path, index=False)
    display_result(result)


def test(json_paths: List[str]):
    n_case = len(json_paths)
    result: Dict[str, List] = {"name": [], "pass": [], "fetch": [], "total": []}
    for i, json_path in enumerate(json_paths):
        name = os.path.basename(json_path)
        print(f"{i}/{n_case}: {name}")
        with open(json_path) as fp:
            try:
                info_dict = process(json.load(fp), name, verbose=True, debug=True)
            except Exception as e:
                raise e
                # print(f"{json_path}: {e.__class__.__name__}(e)")
                # info_dict = None
        if info_dict is None:
            continue
        info_dict["name"] = name
        for k, v in info_dict.items():
            if k in result.keys():
                result[k].append(v)
    display_result(result)


if __name__ == '__main__':
    main(
        "/vepfs/fs_users/caixc/synfacts_results/0408_synfacts_data/results",
        "out/synfact-250417-2.csv", "out/synfact-250417-2.csv", "out/synfact-250415.csv",
    )
    # test([
    #     "/vepfs/fs_users/caixc/synfacts_results/0408_synfacts_data/results/18062cd6a37c403bb4234720ea408580.json",
    # ])
