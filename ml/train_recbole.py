"""Optional RecBole research training: prepare dataset first.

pip install torch recbole
python ml/prepare_recbole.py --snapshot ml/data/eval-v1
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
    if not (root / 'data/commerce/commerce.inter').is_file():
        ap.error('Prepare train-only commerce.inter from a V1.6 snapshot first')
    run_recbole(model=args.model, dataset='commerce', config_file_list=[str(root / 'recbole.yaml')],
                config_dict={'data_path': str(root / 'data')})
