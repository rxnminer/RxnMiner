from rdkit import Chem
from rdkit import RDLogger

RDLogger.DisableLog('rdApp.*')


def is_markush(s: str) -> bool:
    if "<sep>" in s:
        return True
    if Chem.MolFromSmiles(s) is not None:
        return True
    return False
