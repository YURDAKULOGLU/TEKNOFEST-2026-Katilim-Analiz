"""Transactional BDDK registry seeding and validated source-job creation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import UUID

from pydantic import HttpUrl

from katilim_analiz.application.processing import SourceRequest
from katilim_analiz.ingestion.discovery import DiscoveryMode
from katilim_analiz.ingestion.policy import PolicyViolation, validate_url_syntax
from katilim_analiz.ingestion.registry import (
    BankRegistry,
    BankSource,
    RegistryValidationError,
    load_registry,
)
from katilim_analiz.runtime.paths import resolve_runtime_path
from katilim_analiz.storage.database import Database
from katilim_analiz.storage.repositories import JobRepository, SourceRepository

DEFAULT_REGISTRY_PATH = Path("data/registry/bddk-participation-banks-2026-07-18.json")
DEFAULT_CAMPAIGN_REGISTRY_PATH = Path("data/registry/monitored-campaign-sources-2026-07-19.json")
SOURCE_JOB_KIND = "ingest_source"
_CAMPAIGN_REGISTRY_ID = "monitored-campaign-sources"
_REGISTRY_VERSION = re.compile(r"^(\d{4}-\d{2}-\d{2})\.([1-9]\d*)$")


class CampaignSourceStatus(StrEnum):
    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class MonitoredCampaignSource:
    bank_id: str
    status: CampaignSourceStatus
    observed_on: date
    evidence_url: str
    discovery_mode: DiscoveryMode | None = None
    index_url: str | None = None
    detail_path_prefixes: tuple[str, ...] = ()
    max_links: int = 0
    unavailable_reason: str | None = None

    @property
    def verified(self) -> bool:
        return self.status is CampaignSourceStatus.VERIFIED


@dataclass(frozen=True, slots=True)
class MonitoredCampaignSourceRegistry:
    schema_version: str
    registry_id: str
    registry_version: str
    source_observed_on: date
    bank_registry_version: str
    sources: tuple[MonitoredCampaignSource, ...]

    def source(self, bank_id: str) -> MonitoredCampaignSource:
        for source in self.sources:
            if source.bank_id == bank_id:
                return source
        raise KeyError(f"bank is not present in campaign registry: {bank_id}")

    @property
    def verified_sources(self) -> tuple[MonitoredCampaignSource, ...]:
        return tuple(source for source in self.sources if source.verified)

    @property
    def unavailable_sources(self) -> tuple[MonitoredCampaignSource, ...]:
        return tuple(source for source in self.sources if not source.verified)


def load_runtime_registry(path: str | Path | None = None) -> BankRegistry:
    return load_registry(resolve_runtime_path(path or DEFAULT_REGISTRY_PATH))


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RegistryValidationError(f"{context} must be a JSON object")
    return value


def _exact_fields(
    payload: dict[str, Any],
    fields: set[str],
    context: str,
    *,
    optional: set[str] | None = None,
) -> None:
    optional_fields = optional or set()
    unknown = set(payload) - fields - optional_fields
    if unknown:
        raise RegistryValidationError(f"{context} has unknown fields: {sorted(unknown)}")
    missing = fields - set(payload)
    if missing:
        raise RegistryValidationError(f"{context} is missing fields: {sorted(missing)}")


def _string(payload: dict[str, Any], field: str, context: str, *, maximum: int = 1000) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise RegistryValidationError(f"{context}.{field} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise RegistryValidationError(f"{context}.{field} must contain 1..{maximum} characters")
    return cleaned


def _iso_date(payload: dict[str, Any], field: str, context: str) -> date:
    raw = _string(payload, field, context)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise RegistryValidationError(f"{context}.{field} must be an ISO date") from exc


def _official_url(
    payload: dict[str, Any],
    field: str,
    context: str,
    bank: BankSource,
) -> str:
    raw = _string(payload, field, context)
    try:
        canonical, _ = validate_url_syntax(raw, bank)
    except PolicyViolation as exc:
        raise RegistryValidationError(
            f"{context}.{field} is not an approved official URL: {exc.detail}"
        ) from exc
    if canonical != raw:
        raise RegistryValidationError(f"{context}.{field} must already be canonical")
    return canonical


def _detail_prefixes(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RegistryValidationError(f"{context}.detail_path_prefixes must be a string array")
    prefixes = tuple(value)
    if len(prefixes) != len(set(prefixes)):
        raise RegistryValidationError(f"{context}.detail_path_prefixes contains duplicates")
    for prefix in prefixes:
        decoded_prefix = unquote(prefix)
        decoded_segments = decoded_prefix.split("/")
        if (
            not prefix.startswith("/")
            or not prefix.endswith("/")
            or "?" in prefix
            or "#" in prefix
            or "\\" in prefix
            or "\\" in decoded_prefix
            or "//" in prefix
            or "//" in decoded_prefix
            or any(character.isspace() or ord(character) <= 31 for character in decoded_prefix)
            or any(segment in {".", ".."} for segment in decoded_segments)
            or len(prefix) > 500
        ):
            raise RegistryValidationError(
                f"{context}.detail_path_prefixes contains an unsafe path prefix"
            )
    return prefixes


def _parse_campaign_source(
    value: Any,
    position: int,
    bank_registry: BankRegistry,
    observed_on: date,
) -> MonitoredCampaignSource:
    context = f"sources[{position}]"
    payload = _mapping(value, context)
    common = {"bank_id", "status", "observed_on", "evidence_url"}
    bank_id = _string(payload, "bank_id", context, maximum=64)
    try:
        bank = bank_registry.bank(bank_id)
    except KeyError as exc:
        raise RegistryValidationError(f"{context}.bank_id is not in the BDDK registry") from exc
    row_observed_on = _iso_date(payload, "observed_on", context)
    if row_observed_on != observed_on:
        raise RegistryValidationError(f"{context}.observed_on must match source_observed_on")
    evidence_url = _official_url(payload, "evidence_url", context, bank)
    status_raw = _string(payload, "status", context)
    try:
        status = CampaignSourceStatus(status_raw)
    except ValueError as exc:
        raise RegistryValidationError(f"{context}.status is unsupported: {status_raw}") from exc

    if status is CampaignSourceStatus.UNAVAILABLE:
        _exact_fields(payload, common | {"unavailable_reason"}, context)
        return MonitoredCampaignSource(
            bank_id=bank.id,
            status=status,
            observed_on=row_observed_on,
            evidence_url=evidence_url,
            unavailable_reason=_string(payload, "unavailable_reason", context),
        )

    verified_fields = common | {
        "discovery_mode",
        "index_url",
        "detail_path_prefixes",
        "max_links",
    }
    _exact_fields(payload, verified_fields, context)
    try:
        mode = DiscoveryMode(_string(payload, "discovery_mode", context))
    except ValueError as exc:
        raise RegistryValidationError(f"{context}.discovery_mode is unsupported") from exc
    index_url = _official_url(payload, "index_url", context, bank)
    prefixes = _detail_prefixes(payload.get("detail_path_prefixes"), context)
    max_links = payload.get("max_links")
    if isinstance(max_links, bool) or not isinstance(max_links, int):
        raise RegistryValidationError(f"{context}.max_links must be an integer")
    if mode is DiscoveryMode.DETAIL_LINKS:
        if not prefixes or not 1 <= max_links <= 250:
            raise RegistryValidationError(
                f"{context} detail-link mode requires prefixes and max_links in 1..250"
            )
    elif prefixes or max_links != 0:
        raise RegistryValidationError(
            f"{context} unsegmented mode cannot declare detail paths or link outputs"
        )
    return MonitoredCampaignSource(
        bank_id=bank.id,
        status=status,
        observed_on=row_observed_on,
        evidence_url=evidence_url,
        discovery_mode=mode,
        index_url=index_url,
        detail_path_prefixes=prefixes,
        max_links=max_links,
    )


def _parse_campaign_registry(
    value: Any,
    bank_registry: BankRegistry,
) -> MonitoredCampaignSourceRegistry:
    payload = _mapping(value, "registry")
    fields = {
        "schema_version",
        "registry_id",
        "registry_version",
        "source_observed_on",
        "bank_registry_version",
        "bank_count",
        "sources",
    }
    _exact_fields(payload, fields, "registry", optional={"$schema"})
    schema_version = _string(payload, "schema_version", "registry")
    if schema_version != "1.0":
        raise RegistryValidationError(f"unsupported campaign registry schema: {schema_version}")
    registry_id = _string(payload, "registry_id", "registry")
    if registry_id != _CAMPAIGN_REGISTRY_ID:
        raise RegistryValidationError(f"unsupported campaign registry ID: {registry_id}")
    registry_version = _string(payload, "registry_version", "registry")
    version_match = _REGISTRY_VERSION.fullmatch(registry_version)
    if version_match is None:
        raise RegistryValidationError("registry.registry_version must use YYYY-MM-DD.N format")
    observed_on = _iso_date(payload, "source_observed_on", "registry")
    if version_match.group(1) != observed_on.isoformat():
        raise RegistryValidationError("campaign registry version date must match observation date")
    bank_registry_version = _string(payload, "bank_registry_version", "registry")
    if bank_registry_version != bank_registry.registry_version:
        raise RegistryValidationError("campaign registry targets a different BDDK registry version")
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise RegistryValidationError("registry.sources must be an array")
    bank_count = payload.get("bank_count")
    if isinstance(bank_count, bool) or not isinstance(bank_count, int):
        raise RegistryValidationError("registry.bank_count must be an integer")
    if bank_count != len(raw_sources) or bank_count != len(bank_registry.banks):
        raise RegistryValidationError("campaign registry must contain one row per BDDK bank")
    expected_ids = tuple(bank.id for bank in bank_registry.banks)
    actual_ids = tuple(
        _string(_mapping(source, f"sources[{position}]"), "bank_id", f"sources[{position}]")
        for position, source in enumerate(raw_sources)
    )
    if actual_ids != expected_ids:
        raise RegistryValidationError(
            "campaign registry bank IDs and order must exactly match the BDDK registry"
        )
    sources = tuple(
        _parse_campaign_source(source, position, bank_registry, observed_on)
        for position, source in enumerate(raw_sources)
    )
    return MonitoredCampaignSourceRegistry(
        schema_version=schema_version,
        registry_id=registry_id,
        registry_version=registry_version,
        source_observed_on=observed_on,
        bank_registry_version=bank_registry_version,
        sources=sources,
    )


def load_monitored_campaign_registry(
    path: str | Path | None = None,
    *,
    bank_registry: BankRegistry | None = None,
) -> MonitoredCampaignSourceRegistry:
    """Load the strict campaign registry and cross-check its bank/host allowlist."""

    effective_bank_registry = bank_registry or load_runtime_registry()
    registry_path = resolve_runtime_path(path or DEFAULT_CAMPAIGN_REGISTRY_PATH)
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegistryValidationError(
            f"cannot read campaign registry {registry_path}: {exc}"
        ) from exc
    return _parse_campaign_registry(payload, effective_bank_registry)


async def sync_source_registry(database: Database, registry: BankRegistry) -> None:
    """Apply one immutable registry version in one all-or-nothing transaction."""

    async with database.transaction() as session:
        repository = SourceRepository(session)
        for bank in registry.banks:
            await repository.upsert(
                source_id=bank.id,
                registry_version=registry.registry_version,
                listing_order=bank.listing_order,
                legal_name=bank.legal_name,
                homepage_url=bank.listed_homepage_url,
                allowed_hosts=list(bank.allowed_hosts),
                digital_bank=bank.digital_bank,
            )


def build_source_request(
    registry: BankRegistry,
    *,
    bank_id: str,
    source_url: str,
    campaign_key: str | None = None,
) -> SourceRequest:
    """Validate a source against the registry before it can enter the durable queue."""

    bank = registry.bank(bank_id)
    canonical_url, _ = validate_url_syntax(source_url, bank)
    if campaign_key is None:
        digest = hashlib.sha256(canonical_url.encode()).hexdigest()[:24]
        campaign_key = f"{bank.id}:{digest}"
    return SourceRequest(
        bank_id=bank.id,
        bank_name=bank.legal_name,
        source_url=HttpUrl(canonical_url),
        campaign_key=campaign_key,
    )


async def enqueue_source_job(
    database: Database,
    registry: BankRegistry,
    source: SourceRequest,
) -> tuple[UUID, bool]:
    """Persist one bounded typed source job with registry-version idempotency."""

    bank = registry.bank(source.bank_id)
    canonical_url, _ = validate_url_syntax(str(source.source_url), bank)
    if source.bank_name != bank.legal_name or canonical_url != str(source.source_url):
        raise ValueError("source request differs from the active registry identity")
    dedupe_material = f"{registry.registry_version}\0{source.campaign_key}\0{canonical_url}"
    dedupe_key = hashlib.sha256(dedupe_material.encode()).hexdigest()
    async with database.transaction() as session:
        return await JobRepository(session).enqueue(
            SOURCE_JOB_KIND,
            source.model_dump(mode="json", exclude_none=True),
            dedupe_key=dedupe_key,
        )
