import os
import sys
from typing import Tuple, List

import numpy.random as npr
import pandas as pd
from rdkit import RDLogger
from tqdm import tqdm

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from gen_dataset.process import ReactionStorage, AbbrevStorage, process

RDLogger.DisableLog('rdApp.*')
USPTO_CSV_PATH = "/vepfs/fs_unifinder/yangsw/data/markush/gen/ci6b00564_si_002/dataSetB.csv"
ABBREV_CSV_PATH = "../Translator/utils/abbrevs.csv"


def main(
        output_path: str, n_rxn: int, n_try: int,
        fix_sample_num: int = 1,
        ring_sample_num: int = 0,
        atom_site_sample_num: int = 0,
        atom_sub_sample_num: int = 0,
        seed: int = 42,
):
    npr.seed(seed)
    print(f"Processing {output_path}:")
    reaction_storage = ReactionStorage.from_uspto_csv(USPTO_CSV_PATH)
    abbrev_storage = AbbrevStorage.from_csv(ABBREV_CSV_PATH)
    tuples: List[Tuple[str, str]] = []

    def save():
        _s, _t = zip(*tuples)
        _df = pd.DataFrame({"source": _s, "target": _t})
        _df.to_csv(output_path, index=False)

    for i in tqdm(range(n_rxn), total=n_rxn, ncols=60):
        if i and i % 10000 == 0:
            save()
        reaction_smiles = reaction_storage.choice()
        for _ in range(n_try):
            try:
                source, target = process(
                    reaction_smiles, abbrev_storage,
                    fix_sample_num=fix_sample_num, ring_sample_num=ring_sample_num,
                    flexible_site_sample_num=atom_site_sample_num, atom_sub_sample_num=atom_sub_sample_num,
                )
                tuples.append((source, target))
            except (ValueError, AssertionError, AttributeError):
                pass
            except Exception as e:
                if e.__class__.__name__ == "ArgumentError":
                    pass
                else:
                    print(f"{e.__class__.__name__}: {e}")
    save()


def generate_split(
        path: str, name: str, num: int,
        fix_sample_num: int,
        ring_sample_num: int,
        atom_site_sample_num: int,
        atom_sub_sample_num: int,
        seed: int,
):
    base_num = num // 10
    for i, split in enumerate(["train", "valid", "test"]):
        dir_path = os.path.join(path, split)
        os.makedirs(dir_path, exist_ok=True)
        n_rxb = base_num * 8 if i == 0 else base_num
        main(os.path.join(dir_path, f"{name}.csv"), n_rxb, 1,
             fix_sample_num, ring_sample_num, atom_site_sample_num, atom_sub_sample_num, seed * 3 + i)


if __name__ == '__main__':
    root_path = "/vepfs/fs_unifinder/yangsw/data/markush/dataset/250509"
    num = 200000
    # generate_split(root_path, "s1", num, 1, 0, 0, 0, 25050901)
    generate_split(root_path, "s2", num, 2, 0, 0, 0, 25050902)
    generate_split(root_path, "s3", num, 3, 0, 0, 0, 25050903)
    generate_split(root_path, "r1", num, 0, 1, 1, 0, 25050904)
    generate_split(root_path, "r1a1", num, 0, 1, 1, 1, 25050905)
    generate_split(root_path, "s1r1", num, 1, 1, 1, 0, 25050906)
    # main("/vepfs/fs_unifinder/yangsw/data/markush/dataset/250428/train/s1.csv", 200000, 1, 1)
    # main("/vepfs/fs_unifinder/yangsw/data/markush/dataset/250428/train/s2.csv", 150000, 1, 2)
    # main("/vepfs/fs_unifinder/yangsw/data/markush/dataset/250428/train/s3.csv", 100000, 1, 3)
    # main("/vepfs/fs_unifinder/yangsw/data/markush/dataset/250428/valid/s1.csv", 20000, 1, 1)
    # main("/vepfs/fs_unifinder/yangsw/data/markush/dataset/250428/valid/s2.csv", 15000, 1, 2)
    # main("/vepfs/fs_unifinder/yangsw/data/markush/dataset/250428/valid/s3.csv", 10000, 1, 3)
    # main("/vepfs/fs_unifinder/yangsw/data/markush/dataset/250428/test/s1.csv", 20000, 1, 1)
    # main("/vepfs/fs_unifinder/yangsw/data/markush/dataset/250428/test/s2.csv", 15000, 1, 2)
    # main("/vepfs/fs_unifinder/yangsw/data/markush/dataset/250428/test/s3.csv", 10000, 1, 3)
