"""Hash-addressed raw storage and immutable FetchArtifact construction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from katilim_analiz.contracts import FetchArtifact, FetchStatus
from katilim_analiz.ingestion.robots import RobotsDecision

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_SUFFIX = re.compile(r"^\.[a-z0-9]{1,10}$")
_CHALLENGE_MARKERS = (
    b"captcha",
    b"g-recaptcha",
    b"hcaptcha",
    b"cf-chl-",
    b"cloudflare turnstile",
    b"verify you are human",
    b"insan oldugunuzu dogrulayin",
    "insan oldu\u011funuzu do\u011frulay\u0131n".encode(),
)


class ArtifactStoreError(RuntimeError):
    """Raw bytes cannot be persisted without violating immutability."""


class RawArtifactStore(Protocol):
    def put(self, raw_sha256: str, content: bytes, *, suffix: str = ".html") -> str: ...


def _validate_content_address(raw_sha256: str, content: bytes, suffix: str) -> None:
    if not _SHA256.fullmatch(raw_sha256):
        raise ArtifactStoreError("raw_sha256 must be a lowercase SHA-256 digest")
    if hashlib.sha256(content).hexdigest() != raw_sha256:
        raise ArtifactStoreError("content does not match its declared SHA-256")
    if not _SAFE_SUFFIX.fullmatch(suffix):
        raise ArtifactStoreError("artifact suffix is not safe")


def content_key(raw_sha256: str, suffix: str = ".html") -> str:
    if not _SHA256.fullmatch(raw_sha256) or not _SAFE_SUFFIX.fullmatch(suffix):
        raise ArtifactStoreError("invalid content-address components")
    return f"sha256/{raw_sha256[:2]}/{raw_sha256}{suffix}"


class MemoryArtifactStore:
    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}

    @property
    def items(self) -> Mapping[str, bytes]:
        return dict(self._items)

    def put(self, raw_sha256: str, content: bytes, *, suffix: str = ".html") -> str:
        _validate_content_address(raw_sha256, content, suffix)
        existing = self._items.get(raw_sha256)
        if existing is not None and existing != content:
            raise ArtifactStoreError("content address already contains different content")
        self._items[raw_sha256] = bytes(content)
        return content_key(raw_sha256, suffix)

    def read(self, raw_sha256: str) -> bytes | None:
        return self._items.get(raw_sha256)


class PrivateFileArtifactStore:
    """Write raw bytes once under a caller-provided restricted storage root."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    def put(self, raw_sha256: str, content: bytes, *, suffix: str = ".html") -> str:
        _validate_content_address(raw_sha256, content, suffix)
        key = content_key(raw_sha256, suffix)
        destination = self._root / Path(key)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved_parent = destination.parent.resolve()
        if self._root != resolved_parent and self._root not in resolved_parent.parents:
            raise ArtifactStoreError("artifact destination escapes the configured storage root")
        if destination.is_symlink():
            raise ArtifactStoreError("content address cannot be a symbolic link")
        if destination.exists():
            if destination.is_file() and destination.read_bytes() == content:
                return key
            raise ArtifactStoreError("content address already contains different content")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".incoming-", dir=destination.parent, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                if not destination.is_file() or destination.read_bytes() != content:
                    raise ArtifactStoreError(
                        "content address already contains different content"
                    ) from None
            return key
        except OSError as exc:
            raise ArtifactStoreError(f"cannot persist raw artifact: {exc}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def is_html_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    media_type = content_type.split(";", 1)[0].strip().casefold()
    return media_type in {"text/html", "application/xhtml+xml"}


def has_access_challenge(content: bytes) -> bool:
    """Conservatively identify CAPTCHA and interactive authentication responses."""

    lowered = content[:1_000_000].lower()
    if any(marker in lowered for marker in _CHALLENGE_MARKERS):
        return True
    has_password_input = b"<input" in lowered and b"password" in lowered
    has_form = b"<form" in lowered
    return has_form and has_password_input


def _timezone_required(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("fetched_at must include a timezone offset")


def create_fetch_artifact(
    *,
    bank_id: str,
    requested_url: str,
    final_url: str | None,
    status: FetchStatus,
    http_status: int | None,
    fetched_at: datetime,
    robots_allowed: bool,
    content_type: str | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    raw_sha256: str | None = None,
    raw_size_bytes: int = 0,
    private_raw_path: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> FetchArtifact:
    """Create a deterministic event ID and the canonical frozen fetch contract."""

    _timezone_required(fetched_at)
    identity = {
        "bank_id": bank_id,
        "requested_url": requested_url,
        "final_url": final_url,
        "status": status.value,
        "http_status": http_status,
        "fetched_at": fetched_at.isoformat(),
        "robots_allowed": robots_allowed,
        "content_type": content_type,
        "etag": etag,
        "last_modified": last_modified,
        "raw_sha256": raw_sha256,
        "raw_size_bytes": raw_size_bytes,
        "private_raw_path": private_raw_path,
        "error_code": error_code,
        "error_detail": error_detail,
    }
    artifact_digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return FetchArtifact(id=f"fetch:{artifact_digest}", **identity)


@dataclass(frozen=True, slots=True)
class FetchResult:
    artifact: FetchArtifact
    raw_content: bytes | None
    robots_decision: RobotsDecision
