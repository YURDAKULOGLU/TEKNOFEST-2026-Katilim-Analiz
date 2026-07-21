"""Deterministic replay cache for validated local-model answers.

Banks repeat boilerplate sentences across pages and scan runs, so the same
narrow question (same model build, same prompt version, same requested fields,
byte-identical bounded input) keeps reaching the same slow CPU model.  The
answer to an identical question is deterministic at temperature 0 and has
already passed the full acceptance boundary once, so replaying it must not
cost an inference slot.

The cache stores only what the model was allowed to say in the first place:
``(field, quote)`` pairs from a fully validated response.  A replayed answer
is rebuilt as a fresh :class:`ModelExtractionResponse` and pushed through the
exact same validation path as a live answer, including the verbatim grounding
check against the *current* document, so a cached quote that is not a
substring of the current source can never attach evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from katilim_analiz.llm.contracts import MAX_QUOTE_CHARS, ModelFactField

#: Version of the persisted entry layout; bump to invalidate stored entries.
CACHE_SCHEMA_VERSION = "model-response-cache/1.0"

_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class CachedModelAnswer:
    """The validated payload of one accepted model response: quote-only facts."""

    facts: tuple[tuple[ModelFactField, str], ...]

    def __post_init__(self) -> None:
        for _field, quote in self.facts:
            if not quote or len(quote) > MAX_QUOTE_CHARS:
                raise ValueError("cached quote violates the live quote budget")


class ModelResponseCache(Protocol):
    """Small synchronous seam; lookups are pure and never raise on a miss."""

    def get(self, key: str) -> CachedModelAnswer | None: ...

    def put(self, key: str, answer: CachedModelAnswer) -> None: ...


def model_response_cache_key(
    *,
    model_digest: str,
    prompt_version: str,
    requested_fields: frozenset[ModelFactField],
    user_content: str,
) -> str:
    """Key one exact narrow question to one exact model build.

    ``user_content`` is the serialized byte-bounded prompt package, which
    already embeds the content-derived document identity, bank, requested
    fields, and the exact visible block texts — so any change to the source
    content, block windowing, or prompt construction changes the key.
    """

    material = "\0".join(
        (
            model_digest,
            prompt_version,
            ",".join(sorted(field.value for field in requested_fields)),
            user_content,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class InMemoryModelResponseCache:
    """Bounded in-process LRU layer; always safe to enable."""

    def __init__(self, max_entries: int = 4_096) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, CachedModelAnswer] = OrderedDict()

    def get(self, key: str) -> CachedModelAnswer | None:
        answer = self._entries.get(key)
        if answer is not None:
            self._entries.move_to_end(key)
        return answer

    def put(self, key: str, answer: CachedModelAnswer) -> None:
        self._entries[key] = answer
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


class FileModelResponseCache:
    """One JSON file per key under a private directory; anything invalid is a miss.

    The persisted entry is untrusted on read: it must be strict JSON in the
    exact expected shape with a known field enum value and an in-budget quote,
    or it is ignored.  Even a well-formed poisoned entry can only replay into
    the normal validation path, where an ungrounded quote is rejected.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if _KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("cache key must be a lowercase SHA-256 digest")
        return self._directory / f"{key}.json"

    def get(self, key: str) -> CachedModelAnswer | None:
        path = self._path(key)
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, ValueError):
            return None
        return _parse_entry(payload)

    def put(self, key: str, answer: CachedModelAnswer) -> None:
        path = self._path(key)
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "facts": [{"field": field.value, "quote": quote} for field, quote in answer.facts],
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        try:
            handle, temp_name = tempfile.mkstemp(
                dir=self._directory, prefix=f".{key}.", suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    file.write(serialized)
                os.replace(temp_name, path)
            except OSError:
                Path(temp_name).unlink(missing_ok=True)
        except OSError:
            # A full or read-only disk must degrade to a cache miss, never
            # take the extraction path down.
            return


def _parse_entry(payload: object) -> CachedModelAnswer | None:
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "facts"}:
        return None
    if payload["schema_version"] != CACHE_SCHEMA_VERSION:
        return None
    raw_facts = payload["facts"]
    if not isinstance(raw_facts, list):
        return None
    facts: list[tuple[ModelFactField, str]] = []
    for raw in raw_facts:
        if not isinstance(raw, dict) or set(raw) != {"field", "quote"}:
            return None
        raw_field = raw["field"]
        quote = raw["quote"]
        if not isinstance(raw_field, str) or not isinstance(quote, str):
            return None
        if not quote or len(quote) > MAX_QUOTE_CHARS:
            return None
        try:
            field = ModelFactField(raw_field)
        except ValueError:
            return None
        facts.append((field, quote))
    return CachedModelAnswer(facts=tuple(facts))


class LayeredModelResponseCache:
    """Read-through layers, first hit wins; writes go to every layer."""

    def __init__(self, layers: Iterable[ModelResponseCache]) -> None:
        self._layers = tuple(layers)
        if not self._layers:
            raise ValueError("a layered cache requires at least one layer")

    def get(self, key: str) -> CachedModelAnswer | None:
        for index, layer in enumerate(self._layers):
            answer = layer.get(key)
            if answer is not None:
                for earlier in self._layers[:index]:
                    earlier.put(key, answer)
                return answer
        return None

    def put(self, key: str, answer: CachedModelAnswer) -> None:
        for layer in self._layers:
            layer.put(key, answer)


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CachedModelAnswer",
    "FileModelResponseCache",
    "InMemoryModelResponseCache",
    "LayeredModelResponseCache",
    "ModelResponseCache",
    "model_response_cache_key",
]
