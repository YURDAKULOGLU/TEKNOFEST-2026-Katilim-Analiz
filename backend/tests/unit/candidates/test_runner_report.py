"""Report shape tests for the discovery runner (no network, no database)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from katilim_analiz.candidates import (
    BankDiscoveryResult,
    CandidateSuggestion,
    DiscoveryReport,
    run_candidate_discovery,
    write_discovery_report,
)
from katilim_analiz.config import Settings
from katilim_analiz.runtime.composition import RuntimeConfigurationError


def _report(*, dry_run: bool) -> DiscoveryReport:
    suggestion = CandidateSuggestion(
        bank_id="bank-a",
        url="https://bank.example/finansmanlar/konut-finansmani-plus",
        guessed_label="konut",
        matched_tokens=("finansman", "konut"),
        discovered_via="sitemap",
        score=3,
        http_status=None if dry_run else 200,
    )
    return DiscoveryReport(
        generated_at=datetime(2026, 7, 24, 9, 30, tzinfo=UTC),
        bank_registry_version="2026-07-18.2",
        campaign_registry_version="2026-07-24.1",
        dry_run=dry_run,
        banks=(
            BankDiscoveryResult(
                bank_id="bank-a",
                examined_urls=12,
                suggestions=(suggestion,),
                notes=("sitemap was unavailable: https://bank.example/eski.xml",),
            ),
        ),
    )


def test_report_payload_never_declares_auto_enrollment(tmp_path: Path) -> None:
    report = _report(dry_run=False)
    written = write_discovery_report(report, tmp_path)
    assert written == tmp_path / "source-candidates-2026-07-24.json"
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["auto_enrollment"] == "never"
    assert payload["suggestion_count"] == 1
    assert payload["campaign_registry_version"] == "2026-07-24.1"
    bank = payload["banks"][0]
    assert bank["bank_id"] == "bank-a"
    assert bank["suggestions"][0]["guessed_label"] == "konut"
    assert bank["suggestions"][0]["matched_tokens"] == ["finansman", "konut"]
    assert bank["suggestions"][0]["http_status"] == 200


def test_dry_run_report_carries_no_http_status(tmp_path: Path) -> None:
    report = _report(dry_run=True)
    written = write_discovery_report(report, tmp_path)
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["banks"][0]["suggestions"][0]["http_status"] is None


async def test_discovery_refuses_to_run_without_network_consent() -> None:
    settings = Settings(ingest_network_enabled=False)
    with pytest.raises(RuntimeConfigurationError):
        await run_candidate_discovery(settings)
