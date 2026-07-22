"""Deterministic chat answer assembly: relevance, facts, templates and the number gate.

Issue #16 three-layer design. This module owns the two deterministic layers:
retrieval relevance/ranking over already-fetched validated projections, and the
template answer built directly from record fields. The optional model layer
(`AnswerComposerPort`) only rephrases the facts produced here; `answer_is_grounded`
is the hard gate that keeps every number in a composed answer inside the
supplied evidence, mirroring the extraction grounding philosophy (ADR-003).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from katilim_analiz.application.models import CampaignProjection
from katilim_analiz.contracts import (
    AnswerCitation,
    ChatQueryPlan,
    CitationStatus,
    ComparisonDimension,
    EvidenceRef,
    EvidenceStatus,
    QueryIntent,
    RateKind,
    RatePeriod,
)
from katilim_analiz.domain.comparison import ComparisonOutcome, compare_campaigns
from katilim_analiz.domain.normalization import canonicalize_turkish_text

_MAX_ANSWER_CHARS = 5_000
_MAX_CITATIONS = 20
_MIN_KEYWORD_CHARS = 3
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
#: Trailing legal-form tokens that do not distinguish one bank from another.
_GENERIC_NAME_TOKENS = frozenset({"a", "s", "as", "anonim", "sirketi", "bankasi", "turkiye"})

_DIMENSION_LABELS: dict[ComparisonDimension, str] = {
    ComparisonDimension.RATE: "Kâr payı oranı",
    ComparisonDimension.TERM: "Vade",
    ComparisonDimension.FEE: "Ücret",
    ComparisonDimension.REWARD: "Fayda",
    ComparisonDimension.ELIGIBILITY: "Uygunluk",
}


@dataclass(frozen=True, slots=True)
class ComposerFact:
    """One verified statement the composition model may rephrase but not extend."""

    bank_name: str
    product_title: str
    label: str
    value: str
    quote: str | None = None


@dataclass(frozen=True, slots=True)
class AnswerBundle:
    """Deterministic template answer plus the facts and citations behind it."""

    template_answer: str
    facts: tuple[ComposerFact, ...]
    citations: tuple[AnswerCitation, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _FieldFact:
    dimension: ComparisonDimension | None
    label: str
    value: str
    clause: str
    evidence: tuple[EvidenceRef, ...]


def _projection_words(projection: CampaignProjection) -> frozenset[str]:
    data = projection.record.data
    parts = [
        projection.record.data.bank_id.replace("-", " "),
        projection.bank_name,
        data.title,
        data.summary or "",
        *(evidence.quote for evidence in projection.record.evidence),
    ]
    return frozenset(canonicalize_turkish_text(" ".join(parts)).split())


def _keyword_matches(keyword: str, words: frozenset[str]) -> bool:
    # Prefix tolerance covers Turkish suffixes in either direction
    # ("finansmani" ~ "finansman"); very short fragments are never required.
    return any(
        word.startswith(keyword) or keyword.startswith(word)
        for word in words
        if min(len(word), len(keyword)) >= _MIN_KEYWORD_CHARS
    )


def _bank_keys(projection: CampaignProjection) -> frozenset[str]:
    keys = {canonicalize_turkish_text(projection.record.data.bank_id.replace("-", " "))}
    name_tokens = canonicalize_turkish_text(projection.bank_name).split()
    keys.add(" ".join(name_tokens))
    while name_tokens and name_tokens[-1] in _GENERIC_NAME_TOKENS:
        name_tokens.pop()
    keys.add(" ".join(name_tokens))
    return frozenset(key for key in keys if key)


def _dimension_data_present(projection: CampaignProjection, dimension: ComparisonDimension) -> bool:
    data = projection.record.data
    if dimension is ComparisonDimension.RATE:
        return bool(data.rates)
    if dimension is ComparisonDimension.TERM:
        return bool(data.terms)
    if dimension is ComparisonDimension.FEE:
        return bool(data.fees)
    if dimension is ComparisonDimension.REWARD:
        return bool(data.rewards)
    return bool(data.eligibility_conditions or data.customer_segments)


def select_relevant(
    question: str,
    plan: ChatQueryPlan,
    projections: list[CampaignProjection],
) -> list[CampaignProjection]:
    """Deterministically narrow and rank validated projections for one question.

    Keyword relevance is enforced in-process rather than trusting database
    text-search morphology: every residual plan keyword must appear (with
    prefix tolerance) in a record's own bank/title/summary/evidence text. When
    the question names a bank that we can recognize from the retrieved rows,
    other banks are dropped so a single-bank question is never answered with a
    different bank's numbers.
    """

    keywords = [
        canonicalize_turkish_text(keyword)
        for keyword in plan.keywords
        if len(canonicalize_turkish_text(keyword)) >= _MIN_KEYWORD_CHARS
    ]
    surviving = [
        projection
        for projection in projections
        if all(_keyword_matches(keyword, _projection_words(projection)) for keyword in keywords)
    ]

    canonical_question = f" {canonicalize_turkish_text(question)} "
    mentioned = [
        projection
        for projection in surviving
        if any(f" {key} " in canonical_question for key in _bank_keys(projection))
    ]
    if mentioned:
        surviving = mentioned

    def score(projection: CampaignProjection) -> int:
        dimension_hits = sum(
            _dimension_data_present(projection, dimension)
            for dimension in plan.comparison_dimensions
        )
        return 3 * dimension_hits + sum(
            _keyword_matches(keyword, _projection_words(projection)) for keyword in keywords
        )

    ordered = sorted(
        surviving,
        key=lambda item: (
            -score(item),
            -item.record.observed_at.timestamp(),
            item.record.id,
        ),
    )
    return ordered[: plan.limit]


def _stated_evidence(
    projection: CampaignProjection, pointer_prefix: str
) -> tuple[EvidenceRef, ...]:
    alternate = pointer_prefix.removeprefix("/data")
    return tuple(
        sorted(
            (
                evidence
                for evidence in projection.record.evidence
                if evidence.status is EvidenceStatus.STATED
                and (
                    evidence.field_pointer.startswith(pointer_prefix)
                    or evidence.field_pointer.startswith(alternate)
                )
            ),
            key=lambda item: item.id,
        )
    )


def _rate_fact(projection: CampaignProjection) -> _FieldFact | None:
    rates = projection.record.data.rates
    selected = next(
        (
            (index, rate)
            for index, rate in enumerate(rates)
            if rate.kind is RateKind.FINANCING_PROFIT_RATE and rate.period is RatePeriod.MONTHLY
        ),
        None,
    ) or next(((index, rate) for index, rate in enumerate(rates)), None)
    if selected is None:
        return None
    index, rate = selected
    evidence = _stated_evidence(projection, f"/data/rates/{index}")
    if not evidence:
        return None
    return _FieldFact(
        dimension=ComparisonDimension.RATE,
        label="Kâr payı oranı",
        value=rate.raw,
        clause=f"kâr payı oranı {rate.raw} olarak sunulmaktadır",
        evidence=evidence,
    )


def _term_fact(projection: CampaignProjection) -> _FieldFact | None:
    terms = projection.record.data.terms
    if not terms:
        return None
    evidence = _stated_evidence(projection, "/data/terms")
    if not evidence:
        return None
    maximum = max(term.maximum_months for term in terms)
    return _FieldFact(
        dimension=ComparisonDimension.TERM,
        label="Azami vade",
        value=f"{maximum} ay",
        clause=f"{maximum} aya kadar vade imkânı bulunmaktadır",
        evidence=evidence,
    )


def _fee_fact(projection: CampaignProjection) -> _FieldFact | None:
    fees = projection.record.data.fees
    if not fees:
        return None
    evidence = _stated_evidence(projection, "/data/fees")
    if not evidence:
        return None
    value = "; ".join(fee.raw for fee in fees)
    return _FieldFact(
        dimension=ComparisonDimension.FEE,
        label="Ücret",
        value=value,
        clause=f"ücret koşulu kaynakta “{value}” olarak belirtilmiştir",
        evidence=evidence,
    )


def _reward_fact(projection: CampaignProjection) -> _FieldFact | None:
    rewards = projection.record.data.rewards
    if not rewards:
        return None
    evidence = _stated_evidence(projection, "/data/rewards")
    if not evidence:
        return None
    value = "; ".join(reward.raw for reward in rewards)
    return _FieldFact(
        dimension=ComparisonDimension.REWARD,
        label="Fayda",
        value=value,
        clause=f"sunulan fayda kaynakta “{value}” olarak belirtilmiştir",
        evidence=evidence,
    )


def _amount_fact(projection: CampaignProjection) -> _FieldFact | None:
    amounts = projection.record.data.financing_amounts
    if not amounts:
        return None
    evidence = _stated_evidence(projection, "/data/financing_amounts")
    if not evidence:
        return None
    return _FieldFact(
        dimension=None,
        label="Finansman tutarı",
        value=amounts[0].raw,
        clause=f"finansman tutarı {amounts[0].raw} olarak belirtilmiştir",
        evidence=evidence,
    )


_FACT_BUILDERS = {
    ComparisonDimension.RATE: _rate_fact,
    ComparisonDimension.TERM: _term_fact,
    ComparisonDimension.FEE: _fee_fact,
    ComparisonDimension.REWARD: _reward_fact,
}


def _record_facts(
    projection: CampaignProjection,
    dimensions: list[ComparisonDimension],
) -> list[_FieldFact]:
    requested = [dimension for dimension in dimensions if dimension in _FACT_BUILDERS]
    facts = [
        fact
        for dimension in requested
        if (fact := _FACT_BUILDERS[dimension](projection)) is not None
    ]
    if facts:
        # Supplement with the headline price so a term/fee answer still names
        # the stated rate (the spec dialogs pair rate and term in one answer).
        supplements = (_rate_fact(projection), _term_fact(projection))
        for supplement in supplements:
            if supplement is not None and all(fact.label != supplement.label for fact in facts):
                facts.append(supplement)
        return facts[:3]
    # No requested dimension is stated on this record: fall back to its
    # headline facts so a list/detail question still gets the stated values.
    fallback = (_rate_fact(projection), _amount_fact(projection), _term_fact(projection))
    return [fact for fact in fallback if fact is not None][:2]


def _record_sentence(projection: CampaignProjection, facts: list[_FieldFact]) -> str:
    lead = f"{projection.bank_name} {projection.record.data.title} ürününde {facts[0].clause}."
    extra = " ".join(f"Ayrıca {fact.clause}." for fact in facts[1:])
    return f"{lead} {extra}".strip()


def _verdict_sentences(
    projections: list[CampaignProjection],
    dimensions: list[ComparisonDimension],
    as_of: datetime,
    bank_names: dict[str, str],
) -> tuple[list[str], list[str], set[str]]:
    """Rank per dimension; a winner is named only when a same-kind dimension ranks.

    Returns (verdicts, non-comparable notes, comparable evidence ids). ADR-004:
    no cross-dimension score is ever formed; each sentence names one dimension.
    """

    verdicts: list[str] = []
    notes: list[str] = []
    evidence_ids: set[str] = set()
    if len(projections) < 2 or not dimensions:
        return verdicts, notes, evidence_ids
    report = compare_campaigns(
        [item.record for item in projections],
        dimensions,
        as_of=as_of,
    )
    by_dimension: dict[ComparisonDimension, list[ComparisonOutcome]] = {}
    for item in report.items:
        by_dimension.setdefault(item.dimension, []).append(item)
    for dimension, items in sorted(by_dimension.items(), key=lambda entry: entry[0].value):
        label = _DIMENSION_LABELS[dimension]
        ranked = [item for item in items if item.comparable and item.rank is not None]
        if ranked and len(ranked) == len(items):
            winner = min(ranked, key=lambda item: item.rank or 0)
            verdicts.append(
                f"{label} açısından {bank_names[winner.campaign_id]} daha avantajlıdır; "
                f"doğrulanmış değeri: {winner.display_value}."
            )
            for item in ranked:
                evidence_ids.update(item.evidence_ids)
        elif not any(item.comparable for item in items):
            reason = items[0].explanation
            notes.append(f"{label} değerleri aynı bazda sıralanamadı ({reason})")
    return verdicts, notes, evidence_ids


def build_answer_bundle(
    question: str,
    plan: ChatQueryPlan,
    projections: list[CampaignProjection],
    *,
    as_of: datetime,
) -> AnswerBundle | None:
    """Assemble the deterministic answer; ``None`` means no citable fact exists."""

    per_record: list[tuple[CampaignProjection, list[_FieldFact]]] = []
    for projection in projections:
        facts = _record_facts(projection, plan.comparison_dimensions)
        if facts:
            per_record.append((projection, facts))
    if not per_record:
        return None

    warnings: list[str] = []
    verdicts: list[str] = []
    notes: list[str] = []
    if plan.intent is QueryIntent.COMPARE:
        if len(per_record) >= 2:
            verdicts, notes, _ = _verdict_sentences(
                [projection for projection, _ in per_record],
                plan.comparison_dimensions,
                as_of,
                {projection.record.id: projection.bank_name for projection, _ in per_record},
            )
            if not verdicts and plan.comparison_dimensions:
                warnings.append(
                    "Değerler aynı bazda doğrulanamadığı için sıralama yapılmadı; "
                    "banka değerleri yan yana sunuldu."
                )
            elif not plan.comparison_dimensions:
                warnings.append(
                    "Açık bir karşılaştırma boyutu belirtilmediği için sıralama yapılmadı; "
                    "doğrulanmış değerler yan yana sunuldu."
                )
        else:
            warnings.append(
                "Yalnızca bir bankanın doğrulanmış kaydı bulunduğu için karşılaştırma "
                "yapılamadı; mevcut doğrulanmış değerler sunuldu."
            )

    sentences = [*verdicts]
    composer_facts: list[ComposerFact] = [
        ComposerFact(
            bank_name="-",
            product_title="-",
            label="Karşılaştırma sonucu",
            value=verdict,
        )
        for verdict in verdicts
    ]
    citations: dict[str, AnswerCitation] = {}
    answer_length = 0
    for projection, facts in per_record:
        sentence = _record_sentence(projection, facts)
        if answer_length + len(sentence) + 1 > _MAX_ANSWER_CHARS - 600:
            continue
        answer_length += len(sentence) + 1
        sentences.append(sentence)
        for fact in facts:
            composer_facts.append(
                ComposerFact(
                    bank_name=projection.bank_name,
                    product_title=projection.record.data.title,
                    label=fact.label,
                    value=fact.value,
                    quote=fact.evidence[0].quote,
                )
            )
            for evidence in fact.evidence:
                if len(citations) >= _MAX_CITATIONS:
                    break
                citations.setdefault(
                    evidence.id,
                    AnswerCitation(
                        id=evidence.id,
                        source_document_id=evidence.source_document_id,
                        block_id=evidence.block_id,
                        source_url=projection.source_url,
                        source_title=projection.source_title,
                        quote=evidence.quote,
                        status=CitationStatus.VERIFIED,
                    ),
                )
    if len(sentences) <= len(verdicts):
        return None
    sentences.extend(f"Not: {note}." for note in notes)
    return AnswerBundle(
        template_answer=" ".join(sentences)[:_MAX_ANSWER_CHARS].strip(),
        facts=tuple(composer_facts),
        citations=tuple(citations.values()),
        warnings=tuple(warnings),
    )


def _number_tokens(text: str) -> set[str]:
    return {token.replace(",", ".") for token in _NUMBER_RE.findall(text)}


def answer_is_grounded(answer: str, facts: tuple[ComposerFact, ...]) -> bool:
    """Every number in the composed answer must appear in the supplied evidence.

    The check is deliberately strict and format-aware only to the extent of the
    decimal separator: "3,5" and "3.5" are the same number, "3,50" is not. A
    single unmatched number rejects the whole composed answer.
    """

    allowed: set[str] = set()
    for fact in facts:
        allowed |= _number_tokens(fact.value)
        allowed |= _number_tokens(fact.product_title)
        if fact.quote:
            allowed |= _number_tokens(fact.quote)
    return _number_tokens(answer) <= allowed
