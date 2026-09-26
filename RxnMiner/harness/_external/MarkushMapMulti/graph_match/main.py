# import sys
# sys.path.append('/mnt/vepfs/fs_users/caixc/MarkushMapMulti/graph_match')
import os
import json
import time
from rdkit import Chem
import networkx as nx
import multiprocessing

from utils import smiles_to_graph, graph_to_smiles, is_smiles, visualize_graph, visualize_reaction
from map_generic_component import find_best_candidate
from reconstruct_reaction import find_Rp_R_mapping, apply_all_R_groups_with_counts
from Translator import Translator

Translator = Translator()

# 输入、输出目录
INPUT_DIR = '/mnt/vepfs/fs_users/caixc/synfacts_results/cleaned_results'
OUTPUT_DIR = '/mnt/vepfs/fs_users/caixc/synfacts_results/reactions/json'
OUTPUT_IMG_DIR = '/mnt/vepfs/fs_users/caixc/synfacts_results/reactions/imgs'

os.makedirs(os.path.dirname(OUTPUT_DIR), exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_IMG_DIR, exist_ok=True)


def get_reactions(filename):
    """
    原来的处理逻辑不变，只是把 filename 固定写在函数开头的那行删掉，
    改成传进来的参数即可。
    """
    # print(f"Processing file: {filename}")
    img_dir = os.path.join(OUTPUT_IMG_DIR, filename.replace('.json', ''))
    os.makedirs(img_dir, exist_ok=True)
    with open(os.path.join(INPUT_DIR, filename), 'r') as f:
        json_data = json.load(f)

    results = []
    general_reactions = json_data['general_reactions']
    examples = json_data['examples']

    # 预先把所有示例转成图
    G_examples = []
    for example in examples:
        if not is_smiles(example['text']):
            continue
        example['text'] = Translator.refactor(example['text']).caption
        if '<sep>' not in example['text']:
            mol = Chem.MolFromSmiles(example['text'])
            Chem.Kekulize(mol, clearAromaticFlags=True)
            example['text'] = Chem.MolToSmiles(mol, kekuleSmiles=True)
        G_examples.append(smiles_to_graph(example['text']))

    fail = 0
    for general_reaction in general_reactions:
        G_generals = []
        G_products = []
        for i, item in enumerate(general_reaction):
            if is_smiles(item['text']):
                G_generals.append((i, smiles_to_graph(Translator.refactor(item['text']).caption)))
                if item["type"] == 'products':
                    G_products.append(G_generals[-1][1])

        best_match = None
        for example_idx, G_example in enumerate(G_examples):
            try:
                if best_match is None:
                    if len(G_products) == 1:
                        best_match = 0
                    else:
                        best_match, _, _ = find_best_candidate(G_example, G_products)

                G_general = G_products[best_match]
                R_groups = find_Rp_R_mapping(G_example, G_general)

                reaction_item = []
                for i, item in enumerate(G_generals):
                    if item[1] == G_general:
                        reaction_item.append((item[0], G_example))
                    else:
                        new_G = apply_all_R_groups_with_counts(item[1], R_groups, G_general)[0][0]
                        if nx.number_connected_components(new_G) > 1:
                            raise ValueError("New graph has multiple connected components, matching failed.")
                        reaction_item.append((item[0], new_G))

                reaction = []
                removed = set()
                for i, item in enumerate(reaction_item):
                    graph = item[1]
                    smiles = graph_to_smiles(graph)
                    reaction.append({
                        'text': smiles,
                        'relations': general_reaction[item[0]]['relations'] if general_reaction[item[0]]['type'] == 'condition' else [],
                        'type': general_reaction[item[0]]['type'],
                    })
                    if item[1] == G_general:
                        reaction[-1]['relation'] = examples[example_idx]['relations']
                    removed.add(item[0])

                for i, item in enumerate(general_reaction):
                    if i not in removed:
                        reaction.append(item)

                results.append(reaction)
                visualize_reaction(reaction, os.path.join(img_dir, f"{example_idx}.png"))
            except Exception as e:
                fail += 1
                continue

    return results, fail


def _worker_process(queue, filename):
    """
    子进程中真正调用的函数，把结果放到 multiprocessing.Queue 里
    """
    try:
        reactions, fail = get_reactions(filename)
        # 把结果以 dict 的形式塞到 queue
        queue.put({
            'filename': filename,
            'reactions': reactions,
            'fail': fail,
            'success': len(reactions),
        })
    except Exception as e:
        queue.put({
            'filename': filename,
            'error': str(e),
            'reactions': [],
            'fail': 0,
            'success': 0,
        })


def process_with_timeout(filename, timeout_sec=30):
    """
    启动一个子进程去跑 get_reactions，最多等 timeout_sec 秒。
    """
    manager = multiprocessing.Manager()
    q = manager.Queue()

    p = multiprocessing.Process(target=_worker_process, args=(q, filename))
    p.start()
    p.join(timeout_sec)

    result = {}
    if p.is_alive():
        # 子进程还没结束，强制结束它
        p.terminate()
        p.join()
        result['filename'] = filename
        result['timeout'] = True
        result['reactions'] = []
        result['fail'] = 0
        result['success'] = 0
    else:
        # 子进程按时结束，从队列读取结果
        if not q.empty():
            result = q.get()
            result['timeout'] = False
        else:
            # 发生意外，队列空了
            result = {
                'filename': filename,
                'error': 'No result returned from worker',
                'reactions': [],
                'fail': 0,
                'success': 0,
                'timeout': False,
            }
    return result


def main():
    files = []
    for filename in os.listdir(INPUT_DIR):
        if filename.endswith('.json'):
            out_path = os.path.join(OUTPUT_DIR, filename)
            if not os.path.exists(out_path):
                files.append(filename)

    total = 0
    success_count = 0
    fail_count = 0

    for fn in files:
        total += 1
        start_time = time.time()

        # 使用带超时的处理函数
        res = process_with_timeout(fn, timeout_sec=30)

        duration = time.time() - start_time

        if res.get('timeout'):
            print(f"Timeout {fn} after 30s — total: {total}, success: {success_count}, fail: {fail_count}")
            fail_count += 1
            out_content = {
                'filename': fn,
                'timeout': True
            }
        else:
            if res.get('error'):
                print(f"Failed {fn} in {duration:.2f}s — total: {total}, success: {success_count}, fail: {fail_count} — error: {res['error']}")
                out_content = res
                fail_count += 1
            else:
                succ = res.get('success', 0)
                fl = res.get('fail', 0)
                success_count += succ
                fail_count += fl
                print(f"Processed {fn} in {duration:.2f}s, ({succ}/{fl}) — total: {total}, success: {success_count}, fail: {fail_count}")
                out_content = res

        # 把输出写到文件
        out_path = os.path.join(OUTPUT_DIR, fn)
        with open(out_path, 'w') as out_f:
            json.dump(out_content, out_f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
