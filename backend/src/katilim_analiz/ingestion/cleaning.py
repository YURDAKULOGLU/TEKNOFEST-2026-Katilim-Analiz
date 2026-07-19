"""Deterministic HTML cleaning into ordered, evidence-addressable blocks."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime

from bs4 import BeautifulSoup, Comment, Tag

from katilim_analiz.contracts import CleanDocument, FetchArtifact, FetchStatus, SourceBlock

_BLOCK_TEXT_LIMIT = 50_000
_BLOCK_COUNT_LIMIT = 10_000
_REMOVE_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "form",
    "nav",
    "header",
    "footer",
    "aside",
    "dialog",
)
_WHITESPACE = re.compile(r"\s+")
_HIDDEN_STYLE = re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.I)
_NON_CONTENT_CONTAINER_CLASS_TOKENS = frozenset({"bankkart-category-list"})
_NON_CONTENT_CONTAINER_IDS = frozenset({"cookie-dialog-content"})
_PRIMARY_BLOCK_TAGS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "li",
    "tr",
    "blockquote",
    "dt",
    "dd",
}
_LAYOUT_BLOCK_TAGS = {"div", "section", "article"}


class CleaningError(ValueError):
    """Raw source and FetchArtifact cannot produce a trustworthy clean document."""


def _normalize_text(value: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def _class_tokens(tag: Tag) -> frozenset[str]:
    raw_classes = tag.get("class")
    if raw_classes is None:
        return frozenset()
    if isinstance(raw_classes, str):
        return frozenset(value.casefold() for value in raw_classes.split())
    return frozenset(str(value).casefold() for value in raw_classes)


def _locator(tag: Tag) -> str:
    segments: list[str] = []
    current: Tag | None = tag
    while current is not None and current.name != "[document]":
        segment = current.name
        parent = current.parent
        if isinstance(parent, Tag):
            same_name = [child for child in parent.find_all(current.name, recursive=False)]
            if len(same_name) > 1:
                segment += f":nth-of-type({same_name.index(current) + 1})"
        segments.append(segment)
        current = parent if isinstance(parent, Tag) else None
    return " > ".join(reversed(segments))


def _element_text(tag: Tag) -> str:
    if tag.name == "tr":
        cells = tag.find_all(["th", "td"], recursive=False)
        return " | ".join(
            text for cell in cells if (text := _normalize_text(cell.get_text(" ", strip=True)))
        )
    if tag.name == "li":
        parts = [
            child.get_text(" ", strip=True) if isinstance(child, Tag) else str(child)
            for child in tag.contents
            if not isinstance(child, Tag) or child.name not in {"ul", "ol"}
        ]
        return _normalize_text(" ".join(parts))
    return _normalize_text(tag.get_text(" ", strip=True))


def _candidate_elements(root: Tag) -> list[tuple[Tag, str]]:
    candidates: list[tuple[Tag, str]] = []
    for element in root.descendants:
        if not isinstance(element, Tag):
            continue
        if element.name != "tr" and element.find_parent("tr") is not None:
            continue
        if element.name not in {"li", "tr"} and element.find_parent("li") is not None:
            continue
        if element.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            candidates.append((element, "heading"))
        elif element.name in {"p", "blockquote"}:
            candidates.append((element, "paragraph"))
        elif element.name == "li":
            candidates.append((element, "list_item"))
        elif element.name == "tr":
            candidates.append((element, "table"))
        elif element.name in {"dt", "dd"} or (
            element.name in _LAYOUT_BLOCK_TAGS
            and not element.find(_PRIMARY_BLOCK_TAGS | _LAYOUT_BLOCK_TAGS)
        ):
            candidates.append((element, "other"))
    if not candidates and _normalize_text(root.get_text(" ", strip=True)):
        candidates.append((root, "other"))
    return candidates


def _canonical_clean_hash(
    bank_id: str,
    canonical_url: str,
    title: str | None,
    blocks: list[tuple[str, str, str]],
) -> str:
    payload = {
        "bank_id": bank_id,
        "canonical_url": canonical_url,
        "title": title,
        "blocks": [
            {"kind": kind, "locator": locator, "text": text} for kind, text, locator in blocks
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def clean_html(
    fetch_artifact: FetchArtifact,
    raw_html: bytes,
    *,
    cleaned_at: datetime | None = None,
    cleaner_version: str = "html-blocks/1.1",
) -> CleanDocument:
    """Clean one verified raw artefact without extracting or interpreting facts."""

    if fetch_artifact.status is not FetchStatus.SUCCESS:
        raise CleaningError("cleaning requires a successful fetch artifact")
    if not fetch_artifact.robots_allowed:
        raise CleaningError("cleaning requires an affirmative robots decision")
    raw_sha256 = hashlib.sha256(raw_html).hexdigest()
    if raw_sha256 != fetch_artifact.raw_sha256:
        raise CleaningError("raw bytes do not match the FetchArtifact hash")
    if len(raw_html) != fetch_artifact.raw_size_bytes:
        raise CleaningError("raw bytes do not match the FetchArtifact size")
    if fetch_artifact.final_url is None:
        raise CleaningError("successful FetchArtifact has no final URL")
    effective_cleaned_at = cleaned_at or datetime.now(UTC)
    if effective_cleaned_at.tzinfo is None or effective_cleaned_at.utcoffset() is None:
        raise CleaningError("cleaned_at must include a timezone offset")

    soup = BeautifulSoup(raw_html, "html.parser")
    raw_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    normalized_title = _normalize_text(raw_title)
    title = normalized_title[:500] or None
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    for element in soup.find_all(_REMOVE_TAGS):
        element.decompose()
    hidden_elements: list[Tag] = []
    for element in soup.find_all(True):
        class_tokens = _class_tokens(element)
        if (
            element.has_attr("hidden")
            or str(element.get("aria-hidden", "")).casefold() == "true"
            or _HIDDEN_STYLE.search(str(element.get("style", ""))) is not None
            or bool(class_tokens & _NON_CONTENT_CONTAINER_CLASS_TOKENS)
            or str(element.get("id", "")).casefold() in _NON_CONTENT_CONTAINER_IDS
        ):
            hidden_elements.append(element)
    for element in reversed(hidden_elements):
        element.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    if not isinstance(root, Tag):
        raise CleaningError("HTML parser did not produce a document root")

    canonical_blocks: list[tuple[str, str, str]] = []
    for element, kind in _candidate_elements(root):
        text = _element_text(element)
        if not text:
            continue
        locator = _locator(element)
        for chunk_index, start in enumerate(range(0, len(text), _BLOCK_TEXT_LIMIT)):
            chunk = text[start : start + _BLOCK_TEXT_LIMIT]
            chunk_locator = locator
            if len(text) > _BLOCK_TEXT_LIMIT:
                chunk_locator += f"#text-chunk={chunk_index + 1}"
            canonical_blocks.append((kind, chunk, chunk_locator))
            if len(canonical_blocks) > _BLOCK_COUNT_LIMIT:
                raise CleaningError("cleaned document exceeds the evidence block limit")
    if not canonical_blocks:
        raise CleaningError("cleaned document contains no evidence blocks")

    canonical_url = str(fetch_artifact.final_url)
    clean_sha256 = _canonical_clean_hash(
        fetch_artifact.bank_id,
        canonical_url,
        title,
        canonical_blocks,
    )
    blocks: list[SourceBlock] = []
    for ordinal, (kind, text, locator) in enumerate(canonical_blocks):
        text_sha256 = hashlib.sha256(text.encode()).hexdigest()
        block_digest = hashlib.sha256(
            f"{clean_sha256}\0{ordinal}\0{kind}\0{locator}\0{text_sha256}".encode()
        ).hexdigest()
        blocks.append(
            SourceBlock(
                id=f"block:{block_digest}",
                ordinal=ordinal,
                kind=kind,
                text=text,
                locator=locator,
                text_sha256=text_sha256,
            )
        )
    html_tag = soup.find("html")
    language_value = html_tag.get("lang", "") if isinstance(html_tag, Tag) else ""
    language = "tr" if str(language_value).casefold().startswith("tr") else "unknown"
    return CleanDocument(
        id=f"clean:{clean_sha256}",
        fetch_artifact_id=fetch_artifact.id,
        bank_id=fetch_artifact.bank_id,
        canonical_url=canonical_url,
        title=title,
        cleaned_at=effective_cleaned_at,
        cleaner_version=cleaner_version,
        clean_sha256=clean_sha256,
        language=language,
        blocks=blocks,
    )
