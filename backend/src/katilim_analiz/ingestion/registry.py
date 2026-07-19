"""Versioned, deterministic loading of approved BDDK bank sources."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_BANK_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REGISTRY_VERSION = re.compile(r"^(\d{4}-\d{2}-\d{2})\.([1-9]\d*)$")
_REGISTRY_ID = "bddk-participation-banks"
_REGISTRY_SOURCE_URL = "https://www.bddk.org.tr/Kurulus/Liste/77"
_V1_BANK_COUNT = 10
_ROOT_FIELDS = {
    "$schema",
    "schema_version",
    "registry_id",
    "registry_version",
    "source_observed_on",
    "source_url",
    "bank_count",
    "banks",
}
_BANK_FIELDS = {
    "id",
    "listing_order",
    "legal_name",
    "listed_homepage_url",
    "allowed_hosts",
    "digital_bank",
}


class RegistryValidationError(ValueError):
    """Raised when a registry cannot safely be used as an allowlist."""


def normalize_host(host: str) -> str:
    """Return a comparable ASCII hostname without a terminal root-label dot."""

    candidate = host.strip().rstrip(".").casefold()
    if not candidate or any(character.isspace() for character in candidate):
        raise RegistryValidationError("host must be a non-empty DNS name")
    try:
        normalized = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise RegistryValidationError(f"invalid internationalized host: {host!r}") from exc
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise RegistryValidationError("registry hosts must be DNS names, not IP literals")
    labels = normalized.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        raise RegistryValidationError(f"invalid DNS host: {host!r}")
    return normalized


@dataclass(frozen=True, slots=True)
class BankSource:
    id: str
    listing_order: int
    legal_name: str
    listed_homepage_url: str
    allowed_hosts: tuple[str, ...]
    digital_bank: bool

    def permits_host(self, host: str) -> bool:
        try:
            normalized = normalize_host(host)
        except RegistryValidationError:
            return False
        return normalized in self.allowed_hosts


@dataclass(frozen=True, slots=True)
class BankRegistry:
    schema_version: str
    registry_id: str
    registry_version: str
    source_observed_on: date
    source_url: str
    banks: tuple[BankSource, ...]

    def bank(self, bank_id: str) -> BankSource:
        for bank in self.banks:
            if bank.id == bank_id:
                return bank
        raise KeyError(f"bank is not present in registry {self.registry_version}: {bank_id}")

    def bank_for_host(self, host: str) -> BankSource | None:
        for bank in self.banks:
            if bank.permits_host(host):
                return bank
        return None


def _expect_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RegistryValidationError(f"{context} must be a JSON object")
    return value


def _expect_exact_fields(payload: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise RegistryValidationError(f"{context} has unknown fields: {sorted(unknown)}")
    required = allowed - {"$schema"}
    missing = required - set(payload)
    if missing:
        raise RegistryValidationError(f"{context} is missing fields: {sorted(missing)}")


def _expect_string(payload: dict[str, Any], field: str, context: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RegistryValidationError(f"{context}.{field} must be a non-empty string")
    return value.strip()


def _parse_http_url(value: str, context: str) -> tuple[str, str]:
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise RegistryValidationError(f"{context} is not a valid HTTP URL") from exc
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise RegistryValidationError(f"{context} must be an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise RegistryValidationError(f"{context} must not contain credentials")
    try:
        _ = parts.port
    except ValueError as exc:
        raise RegistryValidationError(f"{context} has an invalid port") from exc
    return value, normalize_host(parts.hostname)


def _parse_bank(value: Any, position: int) -> BankSource:
    context = f"banks[{position}]"
    payload = _expect_mapping(value, context)
    _expect_exact_fields(payload, _BANK_FIELDS, context)

    bank_id = _expect_string(payload, "id", context)
    if not _BANK_ID.fullmatch(bank_id):
        raise RegistryValidationError(f"{context}.id is not a stable kebab-case identifier")
    listing_order = payload.get("listing_order")
    if isinstance(listing_order, bool) or not isinstance(listing_order, int) or listing_order < 1:
        raise RegistryValidationError(f"{context}.listing_order must be a positive integer")
    legal_name = _expect_string(payload, "legal_name", context)
    homepage, homepage_host = _parse_http_url(
        _expect_string(payload, "listed_homepage_url", context),
        f"{context}.listed_homepage_url",
    )
    raw_hosts = payload.get("allowed_hosts")
    if (
        not isinstance(raw_hosts, list)
        or not raw_hosts
        or not all(isinstance(host, str) for host in raw_hosts)
    ):
        raise RegistryValidationError(f"{context}.allowed_hosts must be a non-empty string array")
    allowed_hosts = tuple(normalize_host(host) for host in raw_hosts)
    if len(allowed_hosts) != len(set(allowed_hosts)):
        raise RegistryValidationError(f"{context}.allowed_hosts contains duplicates")
    if homepage_host not in allowed_hosts:
        raise RegistryValidationError(f"{context} homepage host must be explicitly allowlisted")
    digital_bank = payload.get("digital_bank")
    if not isinstance(digital_bank, bool):
        raise RegistryValidationError(f"{context}.digital_bank must be boolean")
    return BankSource(
        id=bank_id,
        listing_order=listing_order,
        legal_name=legal_name,
        listed_homepage_url=homepage,
        allowed_hosts=allowed_hosts,
        digital_bank=digital_bank,
    )


def _parse_registry(value: Any) -> BankRegistry:
    payload = _expect_mapping(value, "registry")
    _expect_exact_fields(payload, _ROOT_FIELDS, "registry")
    schema_version = _expect_string(payload, "schema_version", "registry")
    if schema_version != "1.0":
        raise RegistryValidationError(f"unsupported registry schema version: {schema_version}")
    registry_id = _expect_string(payload, "registry_id", "registry")
    if registry_id != _REGISTRY_ID:
        raise RegistryValidationError(f"unsupported registry ID: {registry_id}")
    registry_version = _expect_string(payload, "registry_version", "registry")
    version_match = _REGISTRY_VERSION.fullmatch(registry_version)
    if not version_match:
        raise RegistryValidationError("registry.registry_version must use YYYY-MM-DD.N format")
    observed_raw = _expect_string(payload, "source_observed_on", "registry")
    try:
        observed_on = date.fromisoformat(observed_raw)
    except ValueError as exc:
        raise RegistryValidationError("registry.source_observed_on must be an ISO date") from exc
    if version_match.group(1) != observed_raw:
        raise RegistryValidationError("registry version date must match source_observed_on")
    source_url, _ = _parse_http_url(
        _expect_string(payload, "source_url", "registry"), "registry.source_url"
    )
    if source_url != _REGISTRY_SOURCE_URL:
        raise RegistryValidationError("registry.source_url must be the controlling BDDK list")
    raw_banks = payload.get("banks")
    if not isinstance(raw_banks, list) or not raw_banks:
        raise RegistryValidationError("registry.banks must be a non-empty array")
    banks = tuple(_parse_bank(bank, position) for position, bank in enumerate(raw_banks))
    bank_count = payload.get("bank_count")
    if isinstance(bank_count, bool) or not isinstance(bank_count, int) or bank_count != len(banks):
        raise RegistryValidationError("registry.bank_count must equal the number of banks")
    if bank_count != _V1_BANK_COUNT:
        raise RegistryValidationError("registry schema 1.0 requires exactly ten observed banks")
    if [bank.listing_order for bank in banks] != list(range(1, len(banks) + 1)):
        raise RegistryValidationError("bank listing_order values must be contiguous and ordered")
    if len({bank.id for bank in banks}) != len(banks):
        raise RegistryValidationError("bank IDs must be unique")
    if len({bank.legal_name for bank in banks}) != len(banks):
        raise RegistryValidationError("bank legal names must be unique")
    all_hosts = [host for bank in banks for host in bank.allowed_hosts]
    if len(all_hosts) != len(set(all_hosts)):
        raise RegistryValidationError("an allowed host cannot belong to multiple banks")
    return BankRegistry(
        schema_version=schema_version,
        registry_id=registry_id,
        registry_version=registry_version,
        source_observed_on=observed_on,
        source_url=source_url,
        banks=banks,
    )


def load_registry(path: str | Path) -> BankRegistry:
    """Load and strictly validate a bank registry without silently accepting drift."""

    registry_path = Path(path)
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegistryValidationError(f"cannot read registry {registry_path}: {exc}") from exc
    return _parse_registry(payload)
