import importlib.util
from pathlib import Path
from datetime import timezone


def test_review_normalize():
    script = Path(__file__).resolve().parents[1] / 'scripts/import_reviews.py'
    spec=importlib.util.spec_from_file_location('import_reviews', script)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    review = {'user_id':'test-user','parent_asin':'X100','timestamp':1690000000000,'rating':5.0}
    who, pid, at, uid=mod.normalize_review(review)
    assert len(who)==36 and pid=='amazon:X100' and at.tzinfo==timezone.utc
    assert uid==mod.normalize_review(review)[-1]
    assert mod.normalize_review({**review, 'rating':1}) is None
