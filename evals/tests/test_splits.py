from __future__ import annotations

import pytest

from evals.dataset_validation import validate_split_manifest
from evals.loading import DatasetError
from evals.splits import find_split_leakage


def test_near_duplicate_text_cannot_cross_splits() -> None:
    examples = [
        {
            "example_id": "train-a",
            "split": "train",
            "source_excerpt": "Yeni müşterilere ilk harcamada 500 TL nakit iade fırsatı sunulur.",
        },
        {
            "example_id": "test-b",
            "split": "test",
            "source_excerpt": "Yeni müşterilere ilk harcamada 500 TL nakit iade fırsatı sunulmaktadır.",
        },
    ]

    leaks = find_split_leakage(examples, threshold=0.70)

    assert len(leaks) == 1
    assert {leaks[0].left_id, leaks[0].right_id} == {"train-a", "test-b"}


def test_same_split_near_duplicates_are_not_reported_as_cross_split_leakage() -> None:
    examples = [
        {"example_id": "a", "split": "train", "source_excerpt": "aynı kampanya metni"},
        {"example_id": "b", "split": "train", "source_excerpt": "aynı kampanya metni"},
    ]

    assert find_split_leakage(examples) == ()


def test_split_manifest_fails_closed_on_near_duplicate_leakage() -> None:
    examples = [
        {"example_id": "a", "split": "train", "source_excerpt": "aynı kampanya metni"},
        {"example_id": "b", "split": "test", "source_excerpt": "aynı kampanya metni"},
    ]
    manifest = {
        "near_duplicate_threshold": 0.85,
        "splits": {"train": ["a"], "development": [], "test": ["b"]},
    }

    with pytest.raises(DatasetError, match="cross-split"):
        validate_split_manifest(examples, manifest)
