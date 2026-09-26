import json
import os
from tqdm import tqdm
from mapping_result import process_r_group_json

json_dir_path = "/internfs/wangjunjie/json_process/origin_json"
output_dir_path = "/path/to/data"
os.makedirs(output_dir_path, exist_ok=True)

for file in tqdm(os.listdir(json_dir_path), ncols=60):
    file_path = os.path.join(json_dir_path, file)
    output_file_path = os.path.join(output_dir_path, file)
    try:
        with open(file_path, encoding="utf-8") as fp:
            d = json.load(fp)
            d, valid = process_r_group_json(d)
        # if not valid: continue
        with open(output_file_path, "w+", encoding="utf-8") as fp:
            json.dump(d, fp, indent=4)
    except ValueError as e:
        print(f"{file}: {e.__class__.__name__}({e})")
