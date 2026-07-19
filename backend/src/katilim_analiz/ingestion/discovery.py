"""Bounded one-level campaign-detail discovery from verified official indexes."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from katilim_analiz.ingestion.policy import PolicyViolation, validate_url_syntax
from katilim_analiz.ingestion.registry import BankSource

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class DiscoveryMode(StrEnum):
    """Whether an official collection exposes independently addressable details."""

    DETAIL_LINKS = "detail_links"
    UNSEGMENTED_COLLECTION = "unsegmented_collection"


@dataclass(frozen=True, slots=True)
class CampaignIndexDiscoveryResult:
    """Typed index outcome that never conflates index content with a campaign."""

    index_sha256: str
    discovered_detail_targets: tuple[str, ...]
    unchanged_targets: tuple[str, ...]
    source_index_changed: bool
    review_required: bool

    @property
    def all_detail_targets(self) -> tuple[str, ...]:
        return tuple(sorted((*self.discovered_detail_targets, *self.unchanged_targets)))


def _path_is_ambiguous(path: str) -> bool:
    """Reject traversal/backslashes through a small stack of percent encodings."""

    decoded = path
    for _ in range(4):
        if "\\" in decoded or any(segment in {".", ".."} for segment in decoded.split("/")):
            return True
        next_value = unquote(decoded)
        if next_value == decoded:
            return False
        decoded = next_value
    return True


def _validate_prefixes(prefixes: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for prefix in prefixes:
        if (
            not isinstance(prefix, str)
            or not prefix.startswith("/")
            or not prefix.endswith("/")
            or "?" in prefix
            or "#" in prefix
            or "\\" in prefix
            or "//" in prefix
            or any(ord(character) <= 31 for character in prefix)
        ):
            raise ValueError("detail path prefixes must be absolute, visible paths ending in '/'")
        decoded_prefix = unquote(prefix)
        if (
            "//" in decoded_prefix
            or any(character.isspace() for character in decoded_prefix)
            or _path_is_ambiguous(prefix)
        ):
            raise ValueError("detail path prefixes cannot contain traversal segments")
        if prefix in normalized:
            raise ValueError("detail path prefixes must be unique")
        normalized.append(prefix)
    return tuple(normalized)


def _href_has_ambiguous_path(href: str) -> bool:
    if "\\" in href or any(ord(character) <= 31 for character in href):
        return True
    try:
        path = urlsplit(href).path
    except ValueError:
        return True
    return _path_is_ambiguous(path)


def _is_detail_path(path: str, prefixes: Sequence[str]) -> bool:
    return any(path.startswith(prefix) and path != prefix for prefix in prefixes)


def _canonical_known_targets(
    known_targets: Collection[str],
    *,
    bank: BankSource,
    index_host: str,
    index_path: str,
    prefixes: Sequence[str],
) -> set[str]:
    canonical_targets: set[str] = set()
    for target in known_targets:
        canonical, host = validate_url_syntax(target, bank)
        path = urlsplit(canonical).path or "/"
        if (
            host != index_host
            or path == index_path
            or _path_is_ambiguous(path)
            or not _is_detail_path(path, prefixes)
        ):
            raise ValueError("known campaign target is outside the registered index host/path")
        canonical_targets.add(canonical)
    return canonical_targets


def discover_campaign_index(
    html: bytes | str,
    *,
    bank: BankSource,
    index_url: str,
    detail_path_prefixes: Sequence[str],
    max_links: int,
    known_targets: Collection[str] = (),
    previous_index_sha256: str | None = None,
    mode: DiscoveryMode = DiscoveryMode.DETAIL_LINKS,
) -> CampaignIndexDiscoveryResult:
    """Parse only ``a[href]`` values and return bounded canonical detail targets.

    This is intentionally a pure, one-level parser. It never fetches a discovered
    URL, interprets headings as campaigns, or returns the collection URL itself.
    Network-time DNS/IP validation remains mandatory when each returned target is
    fetched by the existing ingestion policy.
    """

    if not isinstance(html, bytes | str):
        raise TypeError("index HTML must be bytes or text")
    if previous_index_sha256 is not None and not _SHA256.fullmatch(previous_index_sha256):
        raise ValueError("previous_index_sha256 must be a lowercase SHA-256 digest")
    if isinstance(max_links, bool) or not isinstance(max_links, int):
        raise ValueError("max_links must be an integer")

    canonical_index, index_host = validate_url_syntax(index_url, bank)
    index_path = urlsplit(canonical_index).path or "/"
    prefixes = _validate_prefixes(detail_path_prefixes)
    if mode is DiscoveryMode.DETAIL_LINKS:
        if not prefixes or max_links <= 0:
            raise ValueError("detail-link discovery requires prefixes and a positive max_links")
    elif prefixes or max_links != 0:
        raise ValueError("unsegmented collections cannot declare detail paths or link outputs")

    raw_html = html if isinstance(html, bytes) else html.encode("utf-8")
    index_sha256 = hashlib.sha256(raw_html).hexdigest()
    source_index_changed = (
        previous_index_sha256 is not None and previous_index_sha256 != index_sha256
    )
    if mode is DiscoveryMode.UNSEGMENTED_COLLECTION:
        return CampaignIndexDiscoveryResult(
            index_sha256=index_sha256,
            discovered_detail_targets=(),
            unchanged_targets=(),
            source_index_changed=source_index_changed,
            review_required=source_index_changed,
        )

    known = _canonical_known_targets(
        known_targets,
        bank=bank,
        index_host=index_host,
        index_path=index_path,
        prefixes=prefixes,
    )
    targets: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str) or not href or _href_has_ambiguous_path(href):
            continue
        try:
            resolved = urljoin(canonical_index, href)
            canonical, host = validate_url_syntax(resolved, bank)
        except (PolicyViolation, ValueError):
            continue
        path = urlsplit(canonical).path or "/"
        if host != index_host or path == index_path or not _is_detail_path(path, prefixes):
            continue
        targets.add(canonical)

    bounded = tuple(sorted(targets)[:max_links])
    return CampaignIndexDiscoveryResult(
        index_sha256=index_sha256,
        discovered_detail_targets=tuple(target for target in bounded if target not in known),
        unchanged_targets=tuple(target for target in bounded if target in known),
        source_index_changed=source_index_changed,
        review_required=False,
    )
