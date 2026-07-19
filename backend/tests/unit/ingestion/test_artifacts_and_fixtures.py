from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from katilim_analiz.contracts import FetchStatus
from katilim_analiz.ingestion.artifacts import (
    ArtifactStoreError,
    MemoryArtifactStore,
    PrivateFileArtifactStore,
)
from katilim_analiz.ingestion.fixtures import FixtureImportError, import_html_fixture
from katilim_analiz.ingestion.policy import HostPolicy
from katilim_analiz.ingestion.registry import load_registry

REGISTRY_PATH = (
    Path(__file__).resolve().parents[4]
    / "data"
    / "registry"
    / "bddk-participation-banks-2026-07-18.json"
)
OBSERVED_AT = datetime(2026, 7, 18, 9, 30, tzinfo=UTC)


def test_manual_fixture_import_creates_a_hash_addressed_immutable_artifact() -> None:
    registry = load_registry(REGISTRY_PATH)
    store = MemoryArtifactStore()
    raw_html = b"<html><body><h1>Kampanya</h1></body></html>"

    artifact = import_html_fixture(
        registry=registry,
        store=store,
        bank_id="kuveyt-turk",
        source_url="https://www.kuveytturk.com.tr/kampanyalar/ornek",
        raw_html=raw_html,
        observed_at=OBSERVED_AT,
        robots_allowed=True,
    )

    digest = hashlib.sha256(raw_html).hexdigest()
    assert artifact.status is FetchStatus.SUCCESS
    assert artifact.raw_sha256 == digest
    assert artifact.private_raw_path == f"sha256/{digest[:2]}/{digest}.html"
    assert artifact.id.startswith("fetch:")
    assert store.read(digest) == raw_html


@pytest.mark.parametrize(
    ("url", "robots_allowed", "html", "message"),
    [
        ("https://attacker.example/fixture", True, b"<p>ok</p>", "allowlisted"),
        ("http://www.kuveytturk.com.tr/fixture", True, b"<p>ok</p>", "HTTPS"),
        ("https://www.kuveytturk.com.tr/fixture", False, b"<p>ok</p>", "robots"),
        (
            "https://www.kuveytturk.com.tr/fixture",
            True,
            b"<html><title>CAPTCHA</title><p>verify you are human</p></html>",
            "challenge",
        ),
    ],
)
def test_manual_fixture_import_never_bypasses_source_or_access_controls(
    url: str,
    robots_allowed: bool,
    html: bytes,
    message: str,
) -> None:
    registry = load_registry(REGISTRY_PATH)

    with pytest.raises(FixtureImportError, match=message):
        import_html_fixture(
            registry=registry,
            store=MemoryArtifactStore(),
            bank_id="kuveyt-turk",
            source_url=url,
            raw_html=html,
            observed_at=OBSERVED_AT,
            robots_allowed=robots_allowed,
        )


def test_manual_fixture_import_checks_declared_hash_time_and_size() -> None:
    registry = load_registry(REGISTRY_PATH)
    arguments = {
        "registry": registry,
        "store": MemoryArtifactStore(),
        "bank_id": "kuveyt-turk",
        "source_url": "https://www.kuveytturk.com.tr/fixture",
        "raw_html": b"<p>fixture</p>",
        "robots_allowed": True,
    }

    with pytest.raises(FixtureImportError, match="timezone"):
        import_html_fixture(**arguments, observed_at=datetime(2026, 7, 18))

    with pytest.raises(FixtureImportError, match="SHA-256"):
        import_html_fixture(**arguments, observed_at=OBSERVED_AT, expected_sha256="0" * 64)

    with pytest.raises(FixtureImportError, match="size limit"):
        import_html_fixture(
            **arguments,
            observed_at=OBSERVED_AT,
            policy=HostPolicy(max_response_bytes=5),
        )


def test_private_file_store_is_content_addressed_and_never_overwrites(tmp_path: Path) -> None:
    store = PrivateFileArtifactStore(tmp_path)
    content = b"immutable source bytes"
    digest = hashlib.sha256(content).hexdigest()

    key = store.put(digest, content, suffix=".html")
    assert key == f"sha256/{digest[:2]}/{digest}.html"
    assert (tmp_path / key).read_bytes() == content
    assert store.put(digest, content, suffix=".html") == key

    (tmp_path / key).write_bytes(b"tampered")
    with pytest.raises(ArtifactStoreError, match="different content"):
        store.put(digest, content, suffix=".html")
