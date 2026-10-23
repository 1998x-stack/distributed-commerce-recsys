# Offline recommender research and fair comparisons (V1.6)

The production storefront uses Gorse with local fallback. Offline model research is optional. Use the **global temporal novel-item snapshot protocol** before reporting numbers for popularity, ItemCF, Gorse or RecBole. It creates one training cutoff, one fixed candidate universe and one set of held-out targets for all models.

```bash
# On the Mac with business DB configured, choose a UTC time cutoff
PYTHONPATH=services/api python ml/evaluation_cli.py prepare \
  --cutoff 2026-10-01T00:00:00Z --output ml/data/eval-v16 --k 20

# Inspect reproducible baselines: ml/data/eval-v16/metrics.json
python ml/evaluation_cli.py score --snapshot ml/data/eval-v16 \
  --name itemcf --predictions ml/data/eval-v16/itemcf.jsonl

# RecBole TRAIN ONLY export; train_recbole.py still has its own native split
python ml/prepare_recbole.py --snapshot ml/data/eval-v16
python ml/train_recbole.py --model BPR
```

For third-party predictions: JSONL, each line `{"user_id":"...","items":["item-1","item-2"]}`. Score with `ml/evaluation_cli.py score`. The scorer rejects duplicates, unknown users, seen products and out-of-catalog items. Missing users are included as misses and model coverage is reported. Saved snapshots include file checksums and a source fingerprint. Keep `ml/data/` private.

**Do not compare RecBole's native metrics with snapshot scores directly.** The optional trainer still performs RecBole's own internal split; generate external predictions on the saved candidate sets and score them through the snapshot evaluator to establish comparability. No automated Gorse/RecBole rank inference adapter is included. Historical review events measure positive feedback rather than real impressions or online lift. More details: [V1.6 guide](../docs/ITERATION_V1_6.md).
