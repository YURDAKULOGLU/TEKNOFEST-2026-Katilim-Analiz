"""Rule-first extraction orchestration with optional local-model fallback."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from katilim_analiz.config import ModelProfile, Settings
from katilim_analiz.contracts import CleanDocument, ExtractionCandidate, SourceBlock
from katilim_analiz.extraction.candidate import CandidateValidationError, build_candidate
from katilim_analiz.extraction.draft import ExtractionDraft
from katilim_analiz.extraction.model_merge import merge_model_response
from katilim_analiz.extraction.rules import extract_rules
from katilim_analiz.extraction.source_hints import apply_registry_static_page_hint
from katilim_analiz.extraction.validation_policy import BASE_REQUIRED_FIELDS, required_fields
from katilim_analiz.llm import (
    PROMPT_VERSION,
    FileModelResponseCache,
    InMemoryModelResponseCache,
    LayeredModelResponseCache,
    ModelExtractionResponse,
    ModelFactField,
    ModelInferenceError,
    ModelInferenceSkipped,
    ModelResponseCache,
    OllamaStructuredClient,
)

EXTRACTOR_VERSION = "hybrid-extractor/1.2"
_TOM_LISTING_PATH = "/kampanyalar.html"
_SEGMENTATION_UNRESOLVED = "campaign_listing_segmentation_unresolved"
_TOM_CAMPAIGN_TITLE_LOCATOR = re.compile(
    r"^(?P<prefix>.+) > div:nth-of-type\((?P<index>[1-9]\d*)\) > div > h[1-6]$"
)


class ModelExtractor(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def model_digest(self) -> str: ...

    async def extract(
        self,
        document: CleanDocument,
        requested_fields: frozenset[ModelFactField],
        *,
        priority_fields: frozenset[ModelFactField] = frozenset(),
    ) -> ModelExtractionResponse: ...


class ExtractionOutcome(StrEnum):
    CANDIDATE = "candidate"
    ABSTAINED = "abstained"


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    outcome: ExtractionOutcome
    candidate: ExtractionCandidate | None
    issues: tuple[str, ...]
    model_attempted: bool
    accepted_model_facts: int


class ExtractionPipeline:
    def __init__(
        self,
        *,
        model: ModelExtractor | None = None,
        model_enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
        extractor_version: str = EXTRACTOR_VERSION,
    ) -> None:
        if model_enabled and model is None:
            raise ValueError("model_enabled requires a configured local model adapter")
        if not extractor_version.strip():
            raise ValueError("extractor_version must not be empty")
        self._model = model
        self._model_enabled = model_enabled
        self._clock = clock or (lambda: datetime.now(UTC))
        self._extractor_version = extractor_version

    async def extract(
        self,
        document: CleanDocument,
        *,
        static_page_label: str | None = None,
    ) -> ExtractionResult:
        segments = _segment_tom_campaign_listing(document)
        if not segments:
            return _abstained(_SEGMENTATION_UNRESOLVED)
        if len(segments) > 1:
            return _abstained("multiple_campaigns_require_extract_many")
        return await self._extract_one(segments[0], static_page_label=static_page_label)

    async def extract_many(self, document: CleanDocument) -> tuple[ExtractionResult, ...]:
        """Extract listing segments independently while preserving parent evidence IDs."""

        segments = _segment_tom_campaign_listing(document)
        if not segments:
            return (_abstained(_SEGMENTATION_UNRESOLVED),)
        results: list[ExtractionResult] = []
        for segment in segments:
            results.append(await self._extract_one(segment))
        return tuple(results)

    async def _extract_one(
        self,
        document: CleanDocument,
        *,
        static_page_label: str | None = None,
    ) -> ExtractionResult:
        started_at = self._clock()
        draft = extract_rules(document)
        # Issue #33: the curated registry label may break a product-sheet's
        # classification ambiguity before the model is asked, so the model's
        # priority questions target the fields the gate still needs.
        draft = apply_registry_static_page_hint(draft, document, static_page_label)
        model_attempted = False
        accepted_model_facts = 0
        if self._model_enabled and self._model is not None and draft.unresolved_fields:
            try:
                response = await self._model.extract(
                    document,
                    draft.unresolved_fields,
                    priority_fields=_gate_priority_fields(draft),
                )
            except ModelInferenceSkipped as exc:
                issue = f"model_skipped:{exc.code}"
                if issue not in draft.issues:
                    draft = replace(draft, issues=(*draft.issues, issue))
            except ModelInferenceError as exc:
                model_attempted = True
                issue = f"{exc.code.value}:rules_fallback"
                if issue not in draft.issues:
                    draft = replace(draft, issues=(*draft.issues, issue))
            else:
                model_attempted = True
                draft, accepted_model_facts = merge_model_response(draft, response, document)
            # A financing profit rate the model just resolved can complete the
            # registry hint's campaign-type inference; the hint is idempotent.
            draft = apply_registry_static_page_hint(draft, document, static_page_label)

        completed_at = self._clock()
        try:
            candidate = build_candidate(
                draft,
                document,
                started_at=started_at,
                completed_at=completed_at,
                extractor_version=self._extractor_version,
                model_id=(
                    self._model.model_id
                    if accepted_model_facts and self._model is not None
                    else None
                ),
                model_digest=(
                    self._model.model_digest
                    if accepted_model_facts and self._model is not None
                    else None
                ),
                prompt_version=PROMPT_VERSION if accepted_model_facts else None,
                model_fact_count=accepted_model_facts,
            )
        except CandidateValidationError as exc:
            issues = (*draft.issues, f"candidate_abstained:{exc.code}")
            return ExtractionResult(
                outcome=ExtractionOutcome.ABSTAINED,
                candidate=None,
                issues=tuple(dict.fromkeys(issues)),
                model_attempted=model_attempted,
                accepted_model_facts=accepted_model_facts,
            )
        return ExtractionResult(
            outcome=ExtractionOutcome.CANDIDATE,
            candidate=candidate,
            issues=tuple(candidate.issues),
            model_attempted=model_attempted,
            accepted_model_facts=accepted_model_facts,
        )


def _gate_priority_fields(draft: ExtractionDraft) -> frozenset[ModelFactField]:
    """Ask the validation gate's required fields before optional ones.

    While the record's classification is still open, the base required fields
    (title, product family, campaign type) are the priority, because nothing
    else can make the gate decidable.  Once both classifications are bound,
    the (family, campaign type) requirement matrix names the exact fields the
    record still needs to become ``validated``.  This only orders the model's
    questions; optional fields keep their turn in later calls.
    """

    if draft.product_family is None or draft.campaign_type is None:
        required = BASE_REQUIRED_FIELDS
    else:
        requirement = required_fields(draft.product_family.value, draft.campaign_type.value)
        if requirement is None:
            return frozenset()
        required = requirement.all_of | requirement.any_of
    return frozenset(required & draft.unresolved_fields)


def _abstained(issue: str) -> ExtractionResult:
    return ExtractionResult(
        outcome=ExtractionOutcome.ABSTAINED,
        candidate=None,
        issues=(issue,),
        model_attempted=False,
        accepted_model_facts=0,
    )


def _segment_tom_campaign_listing(document: CleanDocument) -> tuple[CleanDocument, ...]:
    if (
        document.bank_id != "tom-katilim"
        or urlsplit(str(document.canonical_url)).path.rstrip("/") != _TOM_LISTING_PATH
    ):
        return (document,)

    title_groups: dict[str, dict[int, SourceBlock]] = {}
    for block in document.blocks:
        if block.kind != "heading":
            continue
        match = _TOM_CAMPAIGN_TITLE_LOCATOR.fullmatch(block.locator)
        if match is None:
            continue
        prefix = match.group("prefix")
        index = int(match.group("index"))
        title_groups.setdefault(prefix, {})[index] = block

    repeated_groups = [
        (prefix, titles) for prefix, titles in title_groups.items() if len(titles) > 1
    ]
    if len(repeated_groups) != 1:
        return ()
    prefix, titles = repeated_groups[0]

    segments: list[CleanDocument] = []
    for index, title in sorted(titles.items()):
        content_prefixes = (
            f"{prefix} > div:nth-of-type({index}) >",
            f"{prefix} > section:nth-of-type({index}) >",
        )
        blocks = [block for block in document.blocks if block.locator.startswith(content_prefixes)]
        if not blocks:
            continue
        segments.append(
            document.model_copy(
                update={
                    "title": title.text,
                    "blocks": blocks,
                }
            )
        )
    return tuple(segments) if len(segments) > 1 else ()


def pipeline_from_settings(
    settings: Settings,
    *,
    http_client: httpx.AsyncClient | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ExtractionPipeline:
    """Map deployment config to rules-only or unresolved-field hybrid behavior."""

    model_enabled = settings.model_profile is not ModelProfile.RULES_ONLY
    if not model_enabled:
        return ExtractionPipeline(model_enabled=False, clock=clock)
    cache_layers: list[ModelResponseCache] = [InMemoryModelResponseCache()]
    if settings.model_response_cache_dir is not None:
        cache_layers.append(FileModelResponseCache(settings.model_response_cache_dir))
    model = OllamaStructuredClient(
        base_url=str(settings.ollama_base_url),
        model=settings.ollama_model,
        model_digest=settings.ollama_model_digest,
        timeout_seconds=settings.model_timeout_seconds,
        max_context=settings.model_max_context,
        keep_alive=settings.model_keep_alive,
        concurrency=settings.model_concurrency,
        http_client=http_client,
        response_cache=LayeredModelResponseCache(cache_layers),
    )
    return ExtractionPipeline(
        model=model,
        model_enabled=True,
        clock=clock,
    )


__all__ = [
    "EXTRACTOR_VERSION",
    "ExtractionOutcome",
    "ExtractionPipeline",
    "ExtractionResult",
    "ModelExtractor",
    "pipeline_from_settings",
]
