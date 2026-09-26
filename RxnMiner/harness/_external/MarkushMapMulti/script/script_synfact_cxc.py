import logging
import warnings
from typing import Tuple

logging.getLogger("torch").setLevel(logging.ERROR)
warnings.filterwarnings("ignore")
# sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from markush_map.data.reaction import Synfact, SYNFACT_TYPE

from graph_match.Translator import Translator
refactor = Translator()
def process(d: SYNFACT_TYPE, name: str = "null") -> Tuple[bool, float, float]:
    for reaction in d["general_reactions"]:
        for obj in reaction:
            if obj["type"] == "products" or obj["type"] == "reactants":
                new_caption = refactor.refactor(obj["text"]).caption
                obj["text"] = new_caption
    for example in d["examples"]:
        new_caption = refactor.refactor(example["text"]).caption
        example["text"] = new_caption
    synfact = Synfact.from_json(d)
    dict_fitted_reaction = synfact.fit_reactions_with_examples(use_tqdm=True)
    total_num_case = len(synfact.reactions) * len(synfact.examples)
    fetched_num_case = 0
    passed_num_case = 0
    for (rid, eid), reaction in dict_fitted_reaction.items():
        # print(f"FOR REACTION #{rid} & EXAMPLE #{eid}:")
        # print(reaction.__repr__())
        reactant_smiles = ".".join([c.smiles for c in reaction.reactants])
        products_smiles = ".".join([c.smiles for c in reaction.products])
        fetched_num_case += 1
        if "*" not in reactant_smiles and "*" not in products_smiles:
            passed_num_case += 1
    print(f"\tFor case {name}: {passed_num_case} / {fetched_num_case} / {total_num_case}")
    return passed_num_case == total_num_case, fetched_num_case / total_num_case, passed_num_case / total_num_case


if __name__ == '__main__':
    import os
    import json

    src_dir = '/mnt/vepfs/fs_users/caixc/synfacts_results'
    json_paths = []
    for file in os.listdir(src_dir):
        if not file.endswith('json'):
            continue
        json_paths.append(os.path.join(src_dir, file))
    stata, fetch_ratios, pass_ratios = [], [], []
    for json_path in json_paths:
        with open(json_path) as fp:
            status, fetch_ratio, pass_ratio = process(json.load(fp), os.path.basename(json_path))
            stata.append(status)
            fetch_ratios.append(fetch_ratio)
            pass_ratios.append(pass_ratio)
    print(f"PASSED: {int(sum(stata))} / {len(stata)}")
    print(f"AVERAGE FETCH RATIO: {sum(fetch_ratios) / len(fetch_ratios)}")
    print(f"AVERAGE PASS RATIO: {sum(pass_ratios) / len(pass_ratios)}")
