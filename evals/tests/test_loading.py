from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.loading import DatasetError, load_jsonl_index


def test_duplicate_example_ids_fail_instead_of_last_write_winning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicates.jsonl"
    rows = [{"example_id": "same", "value": 1}, {"example_id": "same", "value": 2}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    with pytest.raises(DatasetError, match="duplicate example_id"):
        load_jsonl_index(path, id_field="example_id")
