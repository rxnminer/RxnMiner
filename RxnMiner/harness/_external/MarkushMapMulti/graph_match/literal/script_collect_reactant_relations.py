import json
import os
from typing import List, Dict
from tqdm import tqdm

json_dir_path = "/internfs/wangjunjie/json_process/origin_json"
output_path = "/path/to/data"

cases: List[Dict] = []
for file in tqdm(os.listdir(json_dir_path), ncols=60):
    file_path = os.path.join(json_dir_path, file)
    try:
        with open(file_path, encoding="utf-8") as fp:
            d = json.load(fp)
            # for d_reaction in d["general_reactions"]:
            #     for d_obj in d_reaction:
            #         if d_obj["type"] == "reactants" or d_obj["type"] == "reactant":
            #             relations, text = d_obj["relations"], d_obj["text"]
            #             if len(relations) == 0:
            #                 continue
            #             cases.append({"text": text, "relations": relations})
            for d_eg in d["examples"]:
                relations, text = d_eg["relations"], d_eg["text"]
                if len(relations) == 0:
                    continue
                if len([r for r in relations if "=" in r]) > 0:
                    cases.append({"text": text, "relations": relations})
    except ValueError as e:
        pass

with open(output_path, "w+", encoding="utf-8") as fp:
    fp.writelines([json.dumps(case) + "\n" for case in cases])
