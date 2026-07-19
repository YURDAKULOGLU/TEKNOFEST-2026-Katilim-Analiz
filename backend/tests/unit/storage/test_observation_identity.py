from __future__ import annotations

import pytest
from pydantic import ValidationError

from katilim_analiz.application.processing import (
    SourceRequest,
    campaign_observation_key,
)


def test_legacy_job_id_is_the_only_implicit_scan_identity() -> None:
    source = SourceRequest(
        bank_id="bank-a",
        bank_name="Banka A",
        source_url="https://bank.example/kampanya",
        campaign_key="bank-a:campaign",
        job_id="durable-job-1",
    )

    assert source.require_scan_run_id() == "durable-job-1"
    assert source.require_observation_key() == campaign_observation_key(
        "durable-job-1",
        "bank-a:campaign",
        "https://bank.example/kampanya",
    )


def test_observation_identity_fails_closed_without_scan_or_job() -> None:
    source = SourceRequest(
        bank_id="bank-a",
        bank_name="Banka A",
        source_url="https://bank.example/kampanya",
        campaign_key="bank-a:campaign",
    )

    with pytest.raises(ValueError, match="scan_run_id or durable job_id"):
        source.require_observation_key()


def test_supplied_observation_key_must_match_all_identity_fields() -> None:
    with pytest.raises(ValidationError, match="observation_key differs"):
        SourceRequest(
            bank_id="bank-a",
            bank_name="Banka A",
            source_url="https://bank.example/kampanya",
            campaign_key="bank-a:campaign",
            scan_run_id="scan-1",
            observation_key="f" * 64,
        )
