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

#: Live QA finding 2: the financing sub-family words are a HARD scope for
#: relevance selection.  Detected again here from the canonicalized question
#: (same token list the planner uses) so the guarantee does not depend on the
#: soft keyword list surviving retrieval relaxation.
_FINANCING_SUBFAMILY_TERMS = ("konut", "tasit", "ihtiyac")

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
    #: False when the asked-about dimension is stated by no selected record,
    #: or when the plan intent is COMPARE. The composer must then be skipped
    #: entirely: handing it neighbouring numbers (amount, rate) invites
    #: attributing them to the absent field, and a composed comparison invites
    #: phrasing a winner claim — the number-grounding gate cannot see
    #: attribution or non-numeric ranking claims, only number presence.
    compose_allowed: bool = True


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
    if not surviving and (
        plan.bank_ids or plan.product_family is not None or plan.campaign_type is not None
    ):
        # Keywords rank, they must not veto: a leftover question word no
        # record states ("azami") would otherwise erase an answer set the
        # plan's structured filters already pinned down.  Records matching at
        # least one keyword keep priority so a sub-family word ("tasit")
        # still scopes the answer to the right product sheet.
        partial = [
            projection
            for projection in projections
            if any(_keyword_matches(keyword, _projection_words(projection)) for keyword in keywords)
        ]
        surviving = partial or list(projections)

    canonical_question = f" {canonicalize_turkish_text(question)} "
    requested_subfamilies = [
        term
        for term in _FINANCING_SUBFAMILY_TERMS
        if re.search(rf"\b{term}", canonical_question) is not None
    ]
    if requested_subfamilies:
        # Live QA finding 2: a question naming a financing sub-family
        # ("tasit finansmani") must never fall back to another sub-family's
        # record ("ihtiyac") of the same bank.  When no surviving record's
        # own words state the requested sub-family, the selection becomes
        # empty and the service abstains instead of cross-answering.
        surviving = [
            projection
            for projection in surviving
            if any(
                _keyword_matches(term, _projection_words(projection))
                for term in requested_subfamilies
            )
        ]
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


def _evidence_with_status(
    projection: CampaignProjection, pointer_prefix: str, status: EvidenceStatus
) -> tuple[EvidenceRef, ...]:
    alternate = pointer_prefix.removeprefix("/data")
    return tuple(
        sorted(
            (
                evidence
                for evidence in projection.record.evidence
                if evidence.status is status
                and (
                    evidence.field_pointer.startswith(pointer_prefix)
                    or evidence.field_pointer.startswith(alternate)
                )
            ),
            key=lambda item: item.id,
        )
    )


def _stated_evidence(
    projection: CampaignProjection, pointer_prefix: str
) -> tuple[EvidenceRef, ...]:
    return _evidence_with_status(projection, pointer_prefix, EvidenceStatus.STATED)


def _inferred_evidence(
    projection: CampaignProjection, pointer_prefix: str
) -> tuple[EvidenceRef, ...]:
    return _evidence_with_status(projection, pointer_prefix, EvidenceStatus.INFERRED)


def _rate_fact(
    projection: CampaignProjection,
    requested_term_months: int | None = None,
) -> _FieldFact | None:
    rates = projection.record.data.rates
    monthly = [
        (index, rate)
        for index, rate in enumerate(rates)
        if rate.kind is RateKind.FINANCING_PROFIT_RATE and rate.period is RatePeriod.MONTHLY
    ]
    term_specific = next(
        (
            (index, rate)
            for index, rate in monthly
            if requested_term_months is not None and rate.term_months == requested_term_months
        ),
        None,
    )
    selected = (
        term_specific
        or next(iter(monthly), None)
        or next(((index, rate) for index, rate in enumerate(rates)), None)
    )
    if selected is None:
        return None
    index, rate = selected
    # A table-bound rate is INFERRED (its meaning came from the header), yet
    # its quote is still the verbatim cell; the inferred refs back the fact
    # when no stated sentence names the rate.
    evidence = _stated_evidence(projection, f"/data/rates/{index}") or _inferred_evidence(
        projection, f"/data/rates/{index}"
    )
    if not evidence:
        return None
    clause = (
        f"{rate.term_months} ay vadede kâr payı oranı {rate.raw} olarak sunulmaktadır"
        if selected is term_specific and rate.term_months is not None
        else f"kâr payı oranı {rate.raw} olarak sunulmaktadır"
    )
    return _FieldFact(
        dimension=ComparisonDimension.RATE,
        label="Kâr payı oranı",
        value=rate.raw,
        clause=clause,
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

#: "36 ay vadede oran yuzde kac?" — the stated month figure selects the
#: matching per-term table rate instead of the headline rate.
_REQUESTED_TERM_MONTHS = re.compile(r"\b(\d{1,4}) ay\b")

_DIMENSION_ABSENCE_NAMES = {
    ComparisonDimension.RATE: "kâr payı oranı",
    ComparisonDimension.TERM: "vade",
    ComparisonDimension.FEE: "ücret",
    ComparisonDimension.REWARD: "ödül/fayda",
}


def _record_facts(
    projection: CampaignProjection,
    dimensions: list[ComparisonDimension],
    requested_term_months: int | None = None,
) -> list[_FieldFact]:
    requested = [dimension for dimension in dimensions if dimension in _FACT_BUILDERS]

    def build(dimension: ComparisonDimension) -> _FieldFact | None:
        if dimension is ComparisonDimension.RATE:
            return _rate_fact(projection, requested_term_months)
        return _FACT_BUILDERS[dimension](projection)

    facts = [fact for dimension in requested if (fact := build(dimension)) is not None]
    if facts:
        # Supplement with the headline price so a term/fee answer still names
        # the stated rate (the spec dialogs pair rate and term in one answer).
        supplements = (_rate_fact(projection, requested_term_months), _term_fact(projection))
        for supplement in supplements:
            if supplement is not None and all(fact.label != supplement.label for fact in facts):
                facts.append(supplement)
        return facts[:3]
    # No requested dimension is stated on this record: fall back to its
    # headline facts so a list/detail question still gets the stated values.
    fallback = (
        _rate_fact(projection, requested_term_months),
        _amount_fact(projection),
        _term_fact(projection),
    )
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

    term_match = _REQUESTED_TERM_MONTHS.search(canonicalize_turkish_text(question))
    requested_term_months = int(term_match.group(1)) if term_match else None
    per_record: list[tuple[CampaignProjection, list[_FieldFact]]] = []
    for projection in projections:
        facts = _record_facts(
            projection, plan.comparison_dimensions, requested_term_months=requested_term_months
        )
        if facts:
            per_record.append((projection, facts))
    if not per_record:
        return None

    requested = [
        dimension for dimension in plan.comparison_dimensions if dimension in _FACT_BUILDERS
    ]
    # Data-level presence, not fact-level: a rate bound through table hints is
    # INFERRED and builds no stated-evidence fact, yet it is in the source —
    # claiming absence for it would be as wrong as the fabrication this guards.
    requested_stated = not requested or any(
        _dimension_data_present(projection, dimension)
        for projection, _ in per_record
        for dimension in requested
    )
    absence_sentences: list[str] = []
    if not requested_stated:
        # ADR-002: the asked-about field has no stated evidence, and silence
        # here reads as the neighbouring numbers answering the question.
        names = ", ".join(_DIMENSION_ABSENCE_NAMES[dimension] for dimension in requested)
        absence_sentences.append(
            f"İncelenen doğrulanmış kayıtlarda {names} bilgisi kaynak kanıtıyla yer almıyor."
        )

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

    sentences = [*absence_sentences, *verdicts]
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
    if len(sentences) <= len(absence_sentences) + len(verdicts):
        return None
    sentences.extend(f"Not: {note}." for note in notes)
    # COMPARE answers are template-only: ranking/winner wording must come from
    # the deterministic verdict sentences (ADR-004). The number gate cannot see
    # non-numeric claims, so a composed "bank X is better" claim would pass it
    # unchecked even when no dimension actually ranked.
    compose_allowed = requested_stated and plan.intent is not QueryIntent.COMPARE
    return AnswerBundle(
        template_answer=" ".join(sentences)[:_MAX_ANSWER_CHARS].strip(),
        facts=tuple(composer_facts),
        citations=tuple(citations.values()),
        warnings=tuple(warnings),
        compose_allowed=compose_allowed,
    )


def _number_tokens(text: str) -> set[str]:
    return {token.replace(",", ".") for token in _NUMBER_RE.findall(text)}


#: Conservative unit annotations recognized directly adjacent to a number
#: token. Anything else leaves the number unit-less (presence-only check).
_UNIT_PERCENT = "percent"
_UNIT_CURRENCY = "currency"
_UNIT_MONTHS = "months"

_PERCENT_BEFORE_RE = re.compile(r"(?:%\s*|(?:\byüzde|\byuzde)\s+)$", re.IGNORECASE)
_PERCENT_AFTER_RE = re.compile(r"\s*%")
_CURRENCY_BEFORE_RE = re.compile(r"₺\s*$")
_CURRENCY_AFTER_RE = re.compile(r"(?:\s*₺|\s+TL\b|\s+lira\b)", re.IGNORECASE)
_MONTHS_AFTER_RE = re.compile(r"\s+aya?\b", re.IGNORECASE)


def _unit_class(text: str, start: int, end: int) -> str | None:
    """Classify the unit annotation immediately adjacent to text[start:end]."""

    prefix = text[:start]
    suffix = text[end:]
    if _PERCENT_BEFORE_RE.search(prefix) or _PERCENT_AFTER_RE.match(suffix):
        return _UNIT_PERCENT
    if _CURRENCY_BEFORE_RE.search(prefix) or _CURRENCY_AFTER_RE.match(suffix):
        return _UNIT_CURRENCY
    if _MONTHS_AFTER_RE.match(suffix):
        return _UNIT_MONTHS
    return None


def _unit_number_tokens(text: str) -> set[tuple[str, str]]:
    """Normalized (number, unit-class) pairs for unit-annotated numbers."""

    pairs: set[tuple[str, str]] = set()
    for match in _NUMBER_RE.finditer(text):
        unit = _unit_class(text, match.start(), match.end())
        if unit is not None:
            pairs.add((match.group().replace(",", "."), unit))
    return pairs


def answer_is_grounded(answer: str, facts: tuple[ComposerFact, ...]) -> bool:
    """Every number in the composed answer must appear in the supplied evidence.

    The check is deliberately strict and format-aware only to the extent of the
    decimal separator: "3,5" and "3.5" are the same number, "3,50" is not. A
    single unmatched number rejects the whole composed answer.

    Numbers carrying a discernible unit annotation ("%3,5", "yüzde 3,5",
    "3,50 TL", "48 ay") are additionally unit-checked: the same normalized
    number must appear with the same unit class somewhere in the fact texts,
    so evidence stating "%3,50" can never ground an answer saying "3,50 TL".
    A number with no discernible unit keeps the presence-only behavior.
    """

    allowed: set[str] = set()
    allowed_units: set[tuple[str, str]] = set()
    for fact in facts:
        for text in (fact.value, fact.product_title, fact.quote or ""):
            allowed |= _number_tokens(text)
            allowed_units |= _unit_number_tokens(text)
    if not _number_tokens(answer) <= allowed:
        return False
    return _unit_number_tokens(answer) <= allowed_units
