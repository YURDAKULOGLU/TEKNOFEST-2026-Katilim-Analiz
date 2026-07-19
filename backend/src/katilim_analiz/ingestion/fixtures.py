"""Explicit offline import path for previously approved HTML fixtures."""

from __future__ import annotations

import hashlib
from datetime import datetime

from katilim_analiz.contracts import FetchArtifact, FetchStatus
from katilim_analiz.ingestion.artifacts import (
    RawArtifactStore,
    create_fetch_artifact,
    has_access_challenge,
    is_html_content_type,
)
from katilim_analiz.ingestion.policy import HostPolicy, PolicyViolation, validate_url_syntax
from katilim_analiz.ingestion.registry import BankRegistry


class FixtureImportError(ValueError):
    """A manual fixture lacks the policy or provenance needed for safe import."""


def import_html_fixture(
    *,
    registry: BankRegistry,
    store: RawArtifactStore,
    bank_id: str,
    source_url: str,
    raw_html: bytes,
    observed_at: datetime,
    robots_allowed: bool,
    expected_sha256: str | None = None,
    content_type: str = "text/html; charset=utf-8",
    policy: HostPolicy | None = None,
) -> FetchArtifact:
    """Import local bytes without granting a filesystem fixture any network authority."""

    try:
        bank = registry.bank(bank_id)
    except KeyError as exc:
        raise FixtureImportError(f"bank is not present in the active registry: {bank_id}") from exc
    try:
        canonical_url, _ = validate_url_syntax(source_url, bank)
    except PolicyViolation as exc:
        if exc.code == "scheme_not_allowed":
            raise FixtureImportError("manual fixture source must use HTTPS") from exc
        if exc.code == "host_not_allowlisted":
            raise FixtureImportError("manual fixture source host must be allowlisted") from exc
        raise FixtureImportError(f"manual fixture source URL is unsafe: {exc.detail}") from exc
    if not robots_allowed:
        raise FixtureImportError("manual fixture requires an affirmative recorded robots decision")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise FixtureImportError("manual fixture observation time must include a timezone")
    if not isinstance(raw_html, bytes) or not raw_html:
        raise FixtureImportError("manual fixture must contain non-empty raw bytes")
    effective_policy = policy or HostPolicy()
    if len(raw_html) > effective_policy.max_response_bytes:
        raise FixtureImportError("manual fixture exceeds the configured size limit")
    if not is_html_content_type(content_type):
        raise FixtureImportError("manual fixture content type must be HTML")
    if has_access_challenge(raw_html):
        raise FixtureImportError("manual fixture contains an authentication or CAPTCHA challenge")
    raw_sha256 = hashlib.sha256(raw_html).hexdigest()
    if expected_sha256 is not None and expected_sha256 != raw_sha256:
        raise FixtureImportError("manual fixture does not match its expected SHA-256")
    private_raw_path = store.put(raw_sha256, raw_html, suffix=".html")
    return create_fetch_artifact(
        bank_id=bank.id,
        requested_url=canonical_url,
        final_url=canonical_url,
        status=FetchStatus.SUCCESS,
        http_status=200,
        fetched_at=observed_at,
        robots_allowed=True,
        content_type=content_type,
        raw_sha256=raw_sha256,
        raw_size_bytes=len(raw_html),
        private_raw_path=private_raw_path,
    )
