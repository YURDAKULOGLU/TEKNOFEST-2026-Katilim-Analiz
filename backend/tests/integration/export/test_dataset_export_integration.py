from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from katilim_analiz.export import export_public_dataset
from katilim_analiz.intake import ingest_human_verified

PROJECT_ROOT = Path(__file__).resolve().parents[4]
TEMPLATE_PATH = PROJECT_ROOT / "datasets/human-verified/ornek-sablon.json"


async def test_dataset_export_writes_validated_records_with_provenance(  # type: ignore[no-untyped-def]
    database, tmp_path
) -> None:
    ingested = await ingest_human_verified(database, intake_path=TEMPLATE_PATH)
    assert ingested.campaign_count == 4

    output = tmp_path / "public" / "katilim-analiz-dataset.json"
    as_of = datetime(2026, 7, 24, 23, 59, tzinfo=UTC)
    result = await export_public_dataset(
        database,
        output_path=output,
        dataset_version="1.0.0",
        as_of=as_of,
    )

    assert result.status == "exported"
    assert result.record_count == 4
    assert result.dataset_version == "1.0.0"
    assert result.generated_at == as_of
    assert Path(result.output_path) == output

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["dataset_id"] == "katilim-analiz-public-dataset"
    assert payload["record_count"] == len(payload["records"]) == 4
    assert [record["campaign_key"] for record in payload["records"]] == sorted(
        record["campaign_key"] for record in payload["records"]
    )
    for record in payload["records"]:
        assert record["campaign_key"].startswith("human:")
        assert record["source_url"].startswith("https://")
        assert record["extraction"]["method"] == "manual"
        assert record["extraction"]["extractor_version"]
        assert record["facts"], "every exported record must carry quoted evidence"
        assert all(fact["quote"] for fact in record["facts"])

    # The database clock path also produces a well-formed dataset; the visible
    # record count depends on the wall clock, so only the envelope is asserted.
    clock_output = tmp_path / "clock" / "katilim-analiz-dataset.json"
    clock_result = await export_public_dataset(
        database, output_path=clock_output, dataset_version="1.0.1"
    )
    assert clock_result.status == "exported"
    assert clock_result.generated_at.utcoffset() is not None
    clock_payload = json.loads(clock_output.read_text(encoding="utf-8"))
    assert clock_payload["record_count"] == len(clock_payload["records"])
