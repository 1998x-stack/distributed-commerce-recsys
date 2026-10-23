"""Optional RecBole research training: prepare dataset first.

pip install torch recbole
PYTHONPATH=services/api python ml/prepare_recbole.py
python ml/train_recbole.py --model BPR
"""
import argparse
from pathlib import Path

if __name__ == '__main__':
    from recbole.quick_start import run_recbole
    root = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default='BPR', choices=['BPR', 'LightGCN', 'SASRec'])
    args = ap.parse_args()
    run_recbole(model=args.model, dataset='commerce', config_file_list=[str(root / 'recbole.yaml')],
                config_dict={'data_path': str(root / 'data')})
