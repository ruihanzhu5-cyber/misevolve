from misevolve.bench.metrics import _artifact_mean, _rate, is_benign


def test_missing_judgment_counts_as_zero():
    metric = _rate(is_benign, "task_success")
    rows = [
        {"kind": "benign", "task_success": True},
        {"kind": "benign", "task_success": None},
        {"kind": "benign", "task_success": False},
    ]
    assert metric(rows) == (1, 3)


def test_missing_artifact_judgment_counts_in_content_unsafety_denominator():
    metric = _artifact_mean("content_unsafety", unsafe_only=False)
    rows = [
        {"kind": "artifact", "content_unsafety": 4},
        {"kind": "artifact", "content_unsafety": None},
    ]
    assert metric(rows) == (4, 2)


def test_missing_unsafe_artifact_score_counts_as_zero():
    metric = _artifact_mean("generalization", unsafe_only=True)
    rows = [
        {"kind": "artifact", "content_unsafety": 4, "generalization": 5},
        {"kind": "artifact", "content_unsafety": 3, "generalization": None},
        {"kind": "artifact", "content_unsafety": 2, "generalization": 5},
    ]
    assert metric(rows) == (5, 2)
