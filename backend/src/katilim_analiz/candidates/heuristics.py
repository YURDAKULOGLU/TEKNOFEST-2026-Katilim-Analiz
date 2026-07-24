"""Deterministic slug heuristics that classify candidate product/campaign URLs.

Everything in this module is pure: no network access, no clock, no persistence.
The runner feeds it robots.txt bodies, sitemap XML, and index HTML it fetched
under the existing ingestion politeness conventions.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from katilim_analiz.ingestion.policy import PolicyViolation, validate_url_syntax
from katilim_analiz.ingestion.registry import BankSource

#: Banks the discovery tool must skip entirely. Automated collection against
#: them is off the table per ADR-005 and the human-verified intake decision:
#: kuveyt-turk, turkiye-finans, and hayat-finans present CAPTCHA/access
#: challenges the collector never bypasses, and adil-katilim publishes no
#: campaign or financing product pages.
DISCOVERY_BLOCKED_BANK_IDS = frozenset(
    {
        "adil-katilim",
        "hayat-finans",
        "kuveyt-turk",
        "turkiye-finans",
    }
)

#: Slug tokens and the registry label each one suggests. Product tokens map to
#: the curated static-page labels (konut/tasit/ihtiyac); the remaining tokens
#: only signal that the page is finance/campaign shaped.
_TOKEN_LABELS: dict[str, str] = {
    "konut": "konut",
    "tasit": "tasit",
    "arac": "tasit",
    "ihtiyac": "ihtiyac",
    "finansman": "finansman",
    "kampanya": "kampanya",
    "kart": "kart",
}

#: Labels that identify a concrete financing product, ranked by preference.
_PRODUCT_LABEL_ORDER = ("konut", "tasit", "ihtiyac")
_GENERIC_LABEL_ORDER = ("kampanya", "kart", "finansman")

#: Turkish suffixes a slug stem may carry (finansmani, kampanyalari, araclar).
_STEM_SUFFIX = re.compile(r"^(?:[iu]|lar[iu]?|ler[iu]?)$")

_SEGMENT_SPLIT = re.compile(r"[^a-z0-9]+")
_SITEMAP_DIRECTIVE = re.compile(r"^\s*sitemap\s*:\s*(\S+)\s*$", re.IGNORECASE)
_SITEMAP_LOC = re.compile(r"<loc>\s*([^<\s][^<]*?)\s*</loc>", re.IGNORECASE)
_MAX_SITEMAP_LOCS = 5_000


@dataclass(frozen=True, slots=True)
class CandidateClassification:
    """Deterministic verdict for one URL path."""

    matched_tokens: tuple[str, ...]
    guessed_label: str | None
    score: int

    @property
    def is_candidate(self) -> bool:
        return self.guessed_label is not None


def _slug_words(path: str) -> tuple[str, ...]:
    return tuple(word for word in _SEGMENT_SPLIT.split(path.casefold()) if word)


def _word_matches_token(word: str, token: str) -> bool:
    if word == token:
        return True
    if not word.startswith(token):
        return False
    return _STEM_SUFFIX.fullmatch(word[len(token) :]) is not None


def classify_candidate_path(path: str) -> CandidateClassification:
    """Classify one URL path with deterministic slug-token heuristics.

    A path is a candidate only when at least one known token appears as a
    whole slug word (allowing common Turkish suffixes). The guessed label
    prefers concrete product tokens over generic campaign/card/financing ones.
    """

    if not isinstance(path, str):
        raise TypeError("path must be a string")
    words = _slug_words(path)
    matched = tuple(
        token
        for token in sorted(_TOKEN_LABELS)
        if any(_word_matches_token(word, token) for word in words)
    )
    if not matched:
        return CandidateClassification((), None, 0)
    labels = {_TOKEN_LABELS[token] for token in matched}
    guessed = next(
        (label for label in (*_PRODUCT_LABEL_ORDER, *_GENERIC_LABEL_ORDER) if label in labels),
        None,
    )
    score = len(matched) + int(any(label in labels for label in _PRODUCT_LABEL_ORDER))
    return CandidateClassification(matched, guessed, score)


def is_discovery_blocked_bank(bank_id: str) -> bool:
    return bank_id in DISCOVERY_BLOCKED_BANK_IDS


def _url_comparison_forms(url: str) -> tuple[str, ...]:
    """Return trailing-slash-insensitive comparison forms of one URL."""

    parts = urlsplit(url)
    path = parts.path or "/"
    trimmed = path.rstrip("/") or "/"
    return tuple(
        {
            f"https://{parts.hostname}{trimmed}",
            f"https://{parts.hostname}{trimmed}/",
        }
    )


def registry_known_urls(urls: Iterable[str]) -> frozenset[str]:
    """Build the exclusion set from URLs the registry already monitors.

    Callers pass every index_url, evidence_url, and static-page URL from the
    monitored campaign registry; comparison ignores a trailing slash so a
    sitemap variant of a monitored page is not resurfaced as a suggestion.
    """

    known: set[str] = set()
    for url in urls:
        known.update(_url_comparison_forms(url))
    return frozenset(known)


def is_known_url(url: str, known: Collection[str]) -> bool:
    return any(form in known for form in _url_comparison_forms(url))


def sitemap_urls_from_robots(robots_text: str) -> tuple[str, ...]:
    """Extract Sitemap directives from a robots.txt body, in file order."""

    found: list[str] = []
    for line in robots_text.splitlines():
        match = _SITEMAP_DIRECTIVE.match(line.split("#", 1)[0])
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return tuple(found)


def urls_from_sitemap_xml(content: str) -> tuple[str, ...]:
    """Extract ``<loc>`` values from sitemap or sitemap-index XML.

    A bounded regular expression keeps this free of XML-parser attack surface;
    sitemap files the tool fetches are already size-capped by the HTTP client.
    """

    found: list[str] = []
    for match in _SITEMAP_LOC.finditer(content):
        value = match.group(1)
        if value not in found:
            found.append(value)
        if len(found) >= _MAX_SITEMAP_LOCS:
            break
    return tuple(found)


def internal_links_from_html(html: str, *, base_url: str, bank: BankSource) -> tuple[str, ...]:
    """Extract same-bank, same-allowlist absolute links from one HTML page.

    Only ``a[href]`` values that canonicalize onto the bank's allowed hosts
    survive; everything else (other hosts, non-HTTPS, malformed) is dropped.
    """

    links: list[str] = []
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str) or not href or "\\" in href:
            continue
        try:
            canonical, _ = validate_url_syntax(urljoin(base_url, href), bank)
        except (PolicyViolation, ValueError):
            continue
        if canonical not in links:
            links.append(canonical)
    return tuple(links)
