import json
import os
import sys
from dataclasses import dataclass
from typing import Dict, List
from copy import deepcopy

import numpy.random as npr
from rdkit import RDLogger
from tqdm import tqdm

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from gen_dataset.process import ReactionStorage, AbbrevStorage, process_synfact

RDLogger.DisableLog('rdApp.*')
USPTO_CSV_PATH = "/path/to/data"
ABBREV_CSV_PATH = "../Translator/utils/abbrevs.csv"


@dataclass
class ConfigDict:
    name: str
    example_sample_num: int
    fix_sample_num: int
    ring_sample_num: int
    flexible_site_sample_num: int
    atom_sub_sample_num: int
    seed: int


def main(output_path: str, n_rxn: int, config: ConfigDict):
    npr.seed(config.seed)
    print(f"Processing {output_path}:")
    reaction_storage = ReactionStorage.from_uspto_csv(USPTO_CSV_PATH)
    abbrev_storage = AbbrevStorage.from_csv(ABBREV_CSV_PATH)
    dicts: List[Dict] = []

    def save():
        with open(output_path, "w+") as fp:
            fp.writelines([(json.dumps(_d) + "\n") for _d in dicts])

    for i in tqdm(range(n_rxn), total=n_rxn, ncols=60):
        if i and i % 1000 == 0:
            save()
        reaction_smiles = reaction_storage.choice()
        try:
            d = process_synfact(
                reaction_smiles, abbrev_storage,
                config.example_sample_num, config.fix_sample_num, config.ring_sample_num,
                config.flexible_site_sample_num, config.atom_sub_sample_num,
                save_reaction_str=True,
            )
            dicts.append(d)
        except (ValueError, AssertionError, AttributeError):
            pass
        except Exception as e:
            if e.__class__.__name__ == "ArgumentError":
                pass
            else:
                print(f"{e.__class__.__name__}: {e}")
    save()


def generate_split(path: str, num: int, config: ConfigDict):
    base_num = num // 10
    for i, split in enumerate(["train", "valid", "test"]):
        dir_path = os.path.join(path, split)
        os.makedirs(dir_path, exist_ok=True)
        n_rxb = base_num * 8 if i == 0 else base_num
        temp_config = deepcopy(config)
        temp_config.name = config.name + "-" + split
        temp_config.seed = config.seed * 3 + i
        main(os.path.join(dir_path, f"{config.name}.jsonl"), n_rxb, temp_config)


if __name__ == '__main__':
    root_path = "/path/to/data"
    config_dicts = [
        ConfigDict("Fix1", 8, 1, 0, 0, 0, 25060601),
        ConfigDict("Fix2", 8, 2, 0, 0, 0, 25060602),
        ConfigDict("Fix3", 8, 3, 0, 0, 0, 25060603),
        ConfigDict("Ring1", 6, 0, 1, 1, 0, 25060604),
        ConfigDict("Ring1Sub1", 6, 0, 1, 1, 1, 25060605),
        ConfigDict("Ring1Fix1", 6, 1, 1, 1, 0, 25060606),
    ]
    default_num = 20
    for cd in config_dicts:
        generate_split(root_path, default_num, cd)
