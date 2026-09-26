import argparse
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Tuple, Dict, List, Optional

from flask import Flask, request, jsonify, Response
from flask_compress import Compress

from mapping_result import RMappingResult, refine_general_reaction_with_examples, fill_general_smiles_with_texts

parser = argparse.ArgumentParser()
parser.add_argument("-p", "--port", type=int, default=50015)
parser.add_argument("-w", "--num-workers", type=int, default=4)
args = parser.parse_args()
print(args, flush=True)

app = Flask(__name__)
compress = Compress()
compress.init_app(app)
executor = ThreadPoolExecutor(max_workers=args.num_workers)


def query_func(
        general_smiles: str,
        example_smiles: str,
) -> Dict:
    print(f"\tReceived {general_smiles} -> {example_smiles}")
    result = RMappingResult.from_mapping(general_smiles, example_smiles)
    return result.to_dict()


def refine_func(
        reactants_smiles: List[str],
        products_smiles: List[str],
        examples_smiles: List[str],
) -> List[Tuple[List[str], List[str]]]:
    print(f"\tReceived {'.'.join(reactants_smiles)}>>{'.'.join(products_smiles)}<<{'.'.join(examples_smiles)}")
    result = refine_general_reaction_with_examples(reactants_smiles, products_smiles, examples_smiles)
    return result


def fill_func(
        general_smiles: str,
        texts: List[str],
) -> List[Tuple[str, List[str]]]:
    print(f"\tReceived {general_smiles}<<{'|'.join(texts)}")
    result = fill_general_smiles_with_texts(general_smiles, texts)
    return result

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify(status="Healthy"), 200


@app.route('/version', methods=['GET'])
def get_version() -> Tuple[Response, int]:
    return jsonify(version="r_mapping_250612"), 200


@app.route('/config', methods=['GET'])
def get_config() -> Tuple[Response, int]:
    return jsonify(
        general_smiles="e-SMILES of product in general reaction",
        example_smiles="SMILES of selected examples",
    ), 200


@app.route('/query', methods=['POST'])
def query_main() -> Response:
    data = request.json
    general_smiles: str = data["general_smiles"]
    example_smiles: str = data["example_smiles"]
    try:
        result_dict: Dict = executor.submit(query_func, general_smiles, example_smiles).result()
    except Exception:
        print(traceback.format_exc(), flush=True)
        # print(f"{e.__class__.__name__}: {e}", flush=True)
        result_dict = {}
    return jsonify(result_dict)


@app.route('/refine', methods=['POST'])
def refine_main() -> Response:
    data = request.json
    reactants_smiles: List[str] = data["reactants_smiles"]
    products_smiles: List[str] = data["products_smiles"]
    examples_smiles: List[str] = data["examples_smiles"]
    try:
        result_list: List[Optional[Tuple[List[str], List[str]]]] = executor.submit(
            refine_func, reactants_smiles, products_smiles, examples_smiles).result()
    except Exception:
        print(traceback.format_exc(), flush=True)
        # print(f"{e.__class__.__name__}: {e}", flush=True)
        result_list = [None for _ in examples_smiles]
    return jsonify(result_list)


@app.route('/fill', methods=['POST'])
def fill_main() -> Response:
    data = request.json
    general_smiles: str = data["general_smiles"]
    texts: List[str] = data["texts"]
    try:
        result_list: List[Tuple[str, List[str]]] = executor.submit(fill_func, general_smiles, texts).result()
    except Exception:
        print(traceback.format_exc(), flush=True)
        # print(f"{e.__class__.__name__}: {e}", flush=True)
        result_list = [(general_smiles, texts)]
    return jsonify(result_list)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=args.port)
