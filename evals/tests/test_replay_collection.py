from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from evals.loading import DatasetError
from evals.replay_collection import replay_collection, write_replay_outputs
from katilim_analiz.ingestion.artifacts import content_key


def _raw(title: str) -> bytes:
    return f"""
    <html lang="tr"><head><title>{title}</title></head>
    <body><main>
      <h1>{title}</h1>
      <p>Bireysel müşterilere özel finansman kâr payı oranı aylık %1,89 uygulanır.</p>
    </main></body></html>
    """.encode()


def _store(private_root: Path, raw_html: bytes) -> tuple[str, Path]:
    digest = hashlib.sha256(raw_html).hexdigest()
    path = private_root / content_key(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw_html)
    return digest, path


def _attempt(
    *,
    bank_id: str,
    digest: str,
    raw_size: int,
    observed_at: str,
) -> dict[str, Any]:
    url = f"https://{bank_id}.example/kampanya"
    return {
        "candidate_kind": "campaign_detail",
        "coverage_status": "success",
        "error_code": None,
        "fetch_status": "success",
        "final_url": url,
        "http_status": 200,
        "observed_at": observed_at,
        "raw_sha256": digest,
        "raw_size_bytes": raw_size,
        "reason": "official_candidate_fetched_and_cleaned",
        "requested_url": url,
        "robots_allowed": True,
    }


def _bank(bank_id: str, attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        "bank_id": bank_id,
        "bank_name": bank_id,
        "campaign_count": 1,
        "observed_at": attempt["observed_at"],
        "reason": "official_candidate_fetched_and_cleaned",
        "source_count": 1,
        "source_url": attempt["requested_url"],
        "status": "success",
        "attempts": [attempt],
    }


def _write_coverage(path: Path, banks: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "registry_version": "test-registry/1",
                "registry_source_url": "https://regulator.example/banks",
                "started_at": "2026-07-18T09:59:00+03:00",
                "completed_at": "2026-07-18T10:05:00+03:00",
                "collector_profile": {"collector_version": "test-collector/1"},
                "banks": banks,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_offline_replay_is_sorted_timestamp_preserving_and_byte_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "private"
    raw_z = _raw("Z Bankası Finansman Kampanyası")
    raw_a = _raw("A Bankası Finansman Kampanyası")
    digest_z, _ = _store(private_root, raw_z)
    digest_a, _ = _store(private_root, raw_a)
    attempt_z = _attempt(
        bank_id="z-bank",
        digest=digest_z,
        raw_size=len(raw_z),
        observed_at="2026-07-18T10:02:03+03:00",
    )
    attempt_a = _attempt(
        bank_id="a-bank",
        digest=digest_a,
        raw_size=len(raw_a),
        observed_at="2026-07-18T10:01:02+03:00",
    )
    coverage_path = tmp_path / "coverage.json"
    _write_coverage(
        coverage_path, [_bank("z-bank", attempt_z), _bank("a-bank", attempt_a)]
    )

    def deny_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline replay attempted network access")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    rows, manifest = await replay_collection(
        coverage_path=coverage_path,
        private_root=private_root,
    )

    assert [row["bank_id"] for row in rows] == ["a-bank", "z-bank"]
    assert [row["observed_at"] for row in rows] == [
        "2026-07-18T10:01:02+03:00",
        "2026-07-18T10:02:03+03:00",
    ]
    assert [row["raw_sha256"] for row in rows] == [digest_a, digest_z]
    assert manifest["generated_at"] == "2026-07-18T10:05:00+03:00"
    assert manifest["record_count"] == 2
    assert manifest["extractor_profile"].startswith("rules-only:")
    assert manifest["replay_profile"] == "offline-coverage-replay/1.0"

    output_root = tmp_path / "explicit-output"
    derived_output = output_root / "campaigns.jsonl"
    manifest_output = output_root / "manifest.json"
    write_replay_outputs(
        derived_output=derived_output,
        manifest_output=manifest_output,
        rows=rows,
        manifest=manifest,
    )
    first_derived = derived_output.read_bytes()
    first_manifest = manifest_output.read_bytes()
    assert sorted(path.name for path in output_root.iterdir()) == [
        "campaigns.jsonl",
        "manifest.json",
    ]

    with pytest.raises(DatasetError, match="already exists"):
        write_replay_outputs(
            derived_output=derived_output,
            manifest_output=manifest_output,
            rows=rows,
            manifest=manifest,
        )
    assert derived_output.read_bytes() == first_derived
    assert manifest_output.read_bytes() == first_manifest

    write_replay_outputs(
        derived_output=derived_output,
        manifest_output=manifest_output,
        rows=rows,
        manifest=manifest,
        force=True,
    )
    assert derived_output.read_bytes() == first_derived
    assert manifest_output.read_bytes() == first_manifest


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "hash_mismatch", "size_mismatch"])
async def test_offline_replay_fails_closed_on_untrusted_raw_artifact(
    tmp_path: Path,
    failure: str,
) -> None:
    private_root = tmp_path / "private"
    raw_html = _raw("Güvenli Finansman Kampanyası")
    digest, raw_path = _store(private_root, raw_html)
    attempt = _attempt(
        bank_id="test-bank",
        digest=digest,
        raw_size=len(raw_html),
        observed_at="2026-07-18T10:01:02+03:00",
    )
    if failure == "missing":
        raw_path.unlink()
    elif failure == "hash_mismatch":
        raw_path.write_bytes(b"tampered")
    else:
        attempt["raw_size_bytes"] = len(raw_html) + 1
    coverage_path = tmp_path / "coverage.json"
    _write_coverage(coverage_path, [_bank("test-bank", attempt)])

    with pytest.raises(
        DatasetError,
        match={
            "missing": "missing raw artifact",
            "hash_mismatch": "SHA-256 mismatch",
            "size_mismatch": "size mismatch",
        }[failure],
    ):
        await replay_collection(coverage_path=coverage_path, private_root=private_root)


@pytest.mark.asyncio
async def test_offline_replay_rejects_ambiguous_successful_attempts(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    raw_html = _raw("Finansman Kampanyası")
    digest, _ = _store(private_root, raw_html)
    first = _attempt(
        bank_id="test-bank",
        digest=digest,
        raw_size=len(raw_html),
        observed_at="2026-07-18T10:01:02+03:00",
    )
    second = {**first, "requested_url": "https://test-bank.example/ikinci"}
    bank = _bank("test-bank", first)
    bank["attempts"] = [first, second]
    coverage_path = tmp_path / "coverage.json"
    _write_coverage(coverage_path, [bank])

    with pytest.raises(DatasetError, match="multiple successful attempts"):
        await replay_collection(coverage_path=coverage_path, private_root=private_root)


def test_output_paths_must_be_distinct_and_explicit(tmp_path: Path) -> None:
    same_path = tmp_path / "same.json"

    with pytest.raises(DatasetError, match="distinct"):
        write_replay_outputs(
            derived_output=same_path,
            manifest_output=same_path,
            rows=[],
            manifest={},
        )

    assert not same_path.exists()
