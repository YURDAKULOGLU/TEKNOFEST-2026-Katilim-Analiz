"""NFC password policy and fail-closed whole-value blocklist loading."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_PASSWORD_CODEPOINTS = 15
MAX_PASSWORD_CODEPOINTS = 128
BLOCKLIST_SCHEMA_VERSION = "1.0"
BLOCKLIST_MATCHING_CONTRACT = "whole-value-nfc-casefold"
BLOCKLIST_COVERAGE_CONTRACT = "bounded-starter-context-list-no-breached-corpus-claim"
MAX_BLOCKLIST_BYTES = 64 * 1024
MAX_BLOCKLIST_ENTRIES = 10_000

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_METADATA_KEYS = {
    "schema_version",
    "list_version",
    "matching",
    "coverage",
    "entry_count",
    "content_sha256",
}


class PasswordPolicyError(ValueError):
    """A password was rejected without reproducing the candidate in the error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BlocklistUnavailableError(RuntimeError):
    """The versioned blocklist cannot be trusted, so bootstrap must stop."""


@dataclass(frozen=True, slots=True)
class PasswordBlocklist:
    """Verified, bounded whole-password deny list.

    This is deliberately not described as a breached-password corpus. Its
    metadata makes the narrower starter/context-list claim machine-checkable.
    """

    list_version: str
    entries: frozenset[str]
    content_sha256: str

    @classmethod
    def load(cls, entries_path: Path, metadata_path: Path) -> PasswordBlocklist:
        try:
            content = entries_path.read_bytes()
            metadata_raw = metadata_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise BlocklistUnavailableError("password blocklist files are unavailable") from exc
        if len(content) > MAX_BLOCKLIST_BYTES or len(metadata_raw.encode("utf-8")) > 4_096:
            raise BlocklistUnavailableError("password blocklist files exceed bounded sizes")

        try:
            metadata_value: Any = json.loads(metadata_raw)
        except json.JSONDecodeError as exc:
            raise BlocklistUnavailableError("password blocklist metadata is invalid JSON") from exc
        if not isinstance(metadata_value, dict) or set(metadata_value) != _METADATA_KEYS:
            raise BlocklistUnavailableError("password blocklist metadata has an invalid schema")
        metadata: dict[str, Any] = metadata_value

        digest = hashlib.sha256(content).hexdigest()
        if metadata.get("schema_version") != BLOCKLIST_SCHEMA_VERSION:
            raise BlocklistUnavailableError("password blocklist schema version is unsupported")
        if metadata.get("matching") != BLOCKLIST_MATCHING_CONTRACT:
            raise BlocklistUnavailableError("password blocklist matching contract is unsupported")
        if metadata.get("coverage") != BLOCKLIST_COVERAGE_CONTRACT:
            raise BlocklistUnavailableError("password blocklist coverage claim is invalid")
        if metadata.get("content_sha256") != digest or not _SHA256.fullmatch(digest):
            raise BlocklistUnavailableError("password blocklist content hash does not match")

        version = metadata.get("list_version")
        entry_count = metadata.get("entry_count")
        if not isinstance(version, str) or not version or len(version) > 64:
            raise BlocklistUnavailableError("password blocklist version is invalid")
        if (
            not isinstance(entry_count, int)
            or isinstance(entry_count, bool)
            or not 0 < entry_count <= MAX_BLOCKLIST_ENTRIES
        ):
            raise BlocklistUnavailableError("password blocklist entry count is invalid")

        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BlocklistUnavailableError("password blocklist is not valid UTF-8") from exc
        lines = decoded.splitlines()
        if len(lines) != entry_count or any(not line or line != line.strip() for line in lines):
            raise BlocklistUnavailableError("password blocklist entries do not match metadata")

        normalized = [blocklist_key(line) for line in lines]
        if len(set(normalized)) != len(normalized):
            raise BlocklistUnavailableError("password blocklist contains duplicate values")
        return cls(
            list_version=version,
            entries=frozenset(normalized),
            content_sha256=digest,
        )

    def blocks(self, normalized_password: str) -> bool:
        """Match only the complete NFC+casefold value; never use substring rules."""

        return blocklist_key(normalized_password) in self.entries


def normalize_password(candidate: str) -> str:
    """Apply the only password composition transform allowed by ADR-010."""

    if not isinstance(candidate, str):
        raise TypeError("password must be a string")
    return unicodedata.normalize("NFC", candidate)


def blocklist_key(value: str) -> str:
    """Return the complete-value blocklist key without trimming or substring changes."""

    return normalize_password(value).casefold()


def validate_password_shape(candidate: str) -> str:
    """Normalize and enforce the 15-128 Unicode code-point contract."""

    normalized = normalize_password(candidate)
    length = len(normalized)
    if length < MIN_PASSWORD_CODEPOINTS:
        raise PasswordPolicyError(
            "too_short",
            f"password must contain at least {MIN_PASSWORD_CODEPOINTS} Unicode code points",
        )
    if length > MAX_PASSWORD_CODEPOINTS:
        raise PasswordPolicyError(
            "too_long",
            f"password must contain at most {MAX_PASSWORD_CODEPOINTS} Unicode code points",
        )
    return normalized


def validate_bootstrap_password(candidate: str, blocklist: PasswordBlocklist) -> str:
    """Return the exact NFC value to hash, or explain the bootstrap rejection."""

    normalized = validate_password_shape(candidate)
    if blocklist.blocks(normalized):
        raise PasswordPolicyError(
            "blocked_value",
            "password is present in the local common/context-specific blocked-value list",
        )
    return normalized
