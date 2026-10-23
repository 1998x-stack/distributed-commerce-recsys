# Optional recommender research track

The production-facing V1 site calls the Gorse cluster and falls back to a deterministic local ranking strategy. RecBole is **optional**, and deliberately does not share a live database with Gorse.

1. Accumulate realistic interaction data (or use permitted historical samples with pseudonymous IDs).
2. Run `PYTHONPATH=services/api python ml/prepare_recbole.py` to create `ml/data/commerce/commerce.inter`.
3. Run `PYTHONPATH=services/api python ml/offline_baselines.py` for popularity and item co-visitation baselines.
4. Install matching compatible PyTorch + RecBole versions in a separate training environment, then `python ml/train_recbole.py --model BPR`; compare with `LightGCN` and `SASRec`.
5. Do not compare models with different time splits or silently train on future interactions. Train and evaluation datasets must remain isolated from online inference.

Known limitations: the dataset derived from a new demo shop will initially be too sparse for meaningful metrics; RecBole models require sufficient real user-item history. Offline hit-rate does not prove online conversion uplift. More robust subsequent experimentation should use explicit train/validation/test windows, negative sampling policy and diversity/coverage metrics.
