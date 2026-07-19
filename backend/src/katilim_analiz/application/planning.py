"""Rule-first, allowlisted chat planning with an optional non-authoritative model candidate."""

from __future__ import annotations

import re
from dataclasses import dataclass

from katilim_analiz.application.ports import ChatPlanCandidatePort
from katilim_analiz.contracts import (
    CampaignType,
    ChatQueryPlan,
    ComparisonDimension,
    ProductFamily,
    QueryIntent,
)
from katilim_analiz.domain.normalization import canonicalize_turkish_text

_BANK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_UNSAFE_PHRASES = (
    "onceki talimatlari unut",
    "talimatlari yok say",
    "sistem mesajini",
    "system prompt",
    "ignore previous",
    "developer message",
    "drop table",
    "select from",
    "sql calistir",
    "arac cagrisi",
    "tool call",
)
_STOP_WORDS = {
    "acaba",
    "bana",
    "bir",
    "bu",
    "icin",
    "ile",
    "kampanya",
    "kampanyalari",
    "kampanyalarini",
    "mi",
    "mı",
    "gore",
    "olan",
    "ve",
}

_PRODUCT_TERMS: tuple[tuple[ProductFamily, tuple[str, ...]], ...] = (
    (ProductFamily.FINANCING, ("finansman", "konut", "tasit", "ihtiyac")),
    (ProductFamily.CARD, ("kart", "kredi karti")),
    (ProductFamily.PARTICIPATION_ACCOUNT, ("katilma hesabi", "kar payi")),
    (ProductFamily.INVESTMENT, ("yatirim", "fon", "altin")),
)
_CAMPAIGN_TERMS: tuple[tuple[CampaignType, tuple[str, ...]], ...] = (
    (CampaignType.CASHBACK, ("nakit iade", "cashback")),
    (CampaignType.DISCOUNT, ("indirim",)),
    (CampaignType.POINTS, ("puan",)),
    (CampaignType.INSTALLMENT, ("taksit",)),
    (CampaignType.FEE_WAIVER, ("ucretsiz", "ucret muafiyeti")),
    (CampaignType.WELCOME, ("hos geldin", "yeni musteri")),
    (CampaignType.PROFIT_SHARE, ("kar payi",)),
    (CampaignType.FINANCING_RATE, ("finansman orani", "kar orani")),
)
_DIMENSION_TERMS: tuple[tuple[ComparisonDimension, tuple[str, ...]], ...] = (
    (ComparisonDimension.RATE, ("oran", "kar payi", "maliyet")),
    (ComparisonDimension.TERM, ("vade", "azami sure", "taksit suresi")),
    (ComparisonDimension.FEE, ("ucret", "masraf")),
    (ComparisonDimension.REWARD, ("odul", "puan", "indirim", "nakit iade")),
    (ComparisonDimension.ELIGIBILITY, ("kosul", "uygunluk", "kimler")),
)
_INTENT_TERMS: tuple[tuple[QueryIntent, tuple[str, ...]], ...] = (
    (QueryIntent.COVERAGE, ("kapsam", "hangi bankalar", "erisilemeyen", "toplanan banka")),
    (QueryIntent.COMPARE, ("karsilastir", "kiyasla", "hangisi daha", "farklari")),
    (QueryIntent.GLOSSARY, ("ne demek", "nedir", "sozluk")),
    (QueryIntent.DETAIL, ("detay", "ayrinti", "kosullari")),
    (QueryIntent.LIST, ("listele", "goster", "kampanya", "urun")),
)
_TERM_DURATION = re.compile(r"(?:^| )\d{1,4} ay(?: |$)")


@dataclass(frozen=True, slots=True)
class PlannedQuery:
    plan: ChatQueryPlan
    warnings: tuple[str, ...] = ()


def _contains_any(text: str, values: tuple[str, ...]) -> bool:
    return any(value in text for value in values)


def _first_match[T](
    text: str,
    mappings: tuple[tuple[T, tuple[str, ...]], ...],
) -> T | None:
    return next((value for value, terms in mappings if _contains_any(text, terms)), None)


def _dimensions(text: str) -> list[ComparisonDimension]:
    values = [value for value, terms in _DIMENSION_TERMS if _contains_any(text, terms)]
    if _TERM_DURATION.search(text) and ComparisonDimension.TERM not in values:
        values.append(ComparisonDimension.TERM)
    return values


def _intent(text: str) -> QueryIntent:
    return _first_match(text, _INTENT_TERMS) or QueryIntent.UNKNOWN


def _consumed_spans(text: str) -> list[tuple[int, int]]:
    mappings = (_INTENT_TERMS, _PRODUCT_TERMS, _CAMPAIGN_TERMS, _DIMENSION_TERMS)
    spans = [
        match.span()
        for mapping in mappings
        for _, terms in mapping
        for term in terms
        for match in re.finditer(re.escape(term), text)
    ]
    spans.extend(match.span() for match in _TERM_DURATION.finditer(text))
    return spans


def _keywords(text: str) -> list[str]:
    consumed_spans = _consumed_spans(text)
    words = [
        match.group()
        for match in re.finditer(r"\S+", text)
        if len(match.group()) >= 2
        and match.group() not in _STOP_WORDS
        and not any(
            match.start() < consumed_end and consumed_start < match.end()
            for consumed_start, consumed_end in consumed_spans
        )
    ]
    return list(dict.fromkeys(words))[:10]


def _candidate_is_safe(candidate: ChatQueryPlan) -> bool:
    if any(not _BANK_ID.fullmatch(bank_id) for bank_id in candidate.bank_ids):
        return False
    canonical_keywords = [canonicalize_turkish_text(keyword) for keyword in candidate.keywords]
    return not any(
        phrase in keyword for keyword in canonical_keywords for phrase in _UNSAFE_PHRASES
    )


class SafeChatPlanner:
    """Produce a typed query plan; never an executable query or tool request."""

    def __init__(self, candidate_provider: ChatPlanCandidatePort | None = None) -> None:
        self._candidate_provider = candidate_provider

    async def plan(self, question: str) -> PlannedQuery:
        canonical = canonicalize_turkish_text(question)
        if any(phrase in canonical for phrase in _UNSAFE_PHRASES):
            return PlannedQuery(
                plan=ChatQueryPlan(intent=QueryIntent.UNKNOWN),
                warnings=("Soru güvenli sorgu planı sınırlarının dışında bırakıldı.",),
            )

        deterministic = ChatQueryPlan(
            intent=_intent(canonical),
            product_family=_first_match(canonical, _PRODUCT_TERMS),
            campaign_type=_first_match(canonical, _CAMPAIGN_TERMS),
            comparison_dimensions=_dimensions(canonical),
            keywords=_keywords(canonical),
            limit=5,
        )
        if deterministic.intent is not QueryIntent.UNKNOWN or self._candidate_provider is None:
            return PlannedQuery(plan=deterministic)

        try:
            candidate = await self._candidate_provider.propose(question)
        except Exception:
            return PlannedQuery(
                plan=deterministic,
                warnings=("Yerel model sorgu planı üretemedi; kural tabanlı plan kullanıldı.",),
            )
        if candidate is None:
            return PlannedQuery(plan=deterministic)
        if not _candidate_is_safe(candidate):
            return PlannedQuery(
                plan=deterministic,
                warnings=("Yerel modelin sorgu planı güvenlik doğrulamasını geçemedi.",),
            )
        return PlannedQuery(plan=candidate)
