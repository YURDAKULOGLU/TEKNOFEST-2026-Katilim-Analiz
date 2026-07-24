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
    "avantajli",
    "bana",
    "banka",
    "bankada",
    "bankalar",
    "bankalarda",
    "bankanin",
    "bankasi",
    "bankasinda",
    "bankasinin",
    "bir",
    "bu",
    "daha",
    # Canonicalization splits suffixed apostrophes into bare case-suffix
    # tokens ("Katilim'in" -> "katilim in"); they carry no search meaning.
    "da",
    "dan",
    "de",
    "den",
    "en",
    "hangi",
    "hangisi",
    "icin",
    "ile",
    "in",
    "kac",
    "kadar",
    "kampanya",
    "kampanyalari",
    "kampanyalarini",
    "mi",
    "mı",
    "mu",
    "ne",
    "nin",
    "nun",
    "gore",
    "olan",
    "sunuyor",
    "un",
    "uygun",
    "var",
    "ve",
}

#: Issue #16 follow-up: a bank named in the question is a structured filter,
#: not a free-text keyword.  Patterns run over the canonicalized (ascii,
#: punctuation-free) question; the optional "katilim/bankasi" tails are
#: consumed so their tokens do not leak into the keyword list.
_BANK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("albaraka-turk", re.compile(r"\balbaraka\b(?: turk\w*)?(?: katilim\w*)?(?: bankasi\w*)?")),
    ("adil-katilim", re.compile(r"\badil katilim\w*(?: bankasi\w*)?")),
    ("dunya-katilim", re.compile(r"\bdunya katilim\w*(?: bankasi\w*)?")),
    ("emlak-katilim", re.compile(r"\bemlak\b(?: katilim\w*)?(?: bankasi\w*)?")),
    ("hayat-finans", re.compile(r"\bhayat finans\w*(?: katilim\w*)?(?: bankasi\w*)?")),
    ("kuveyt-turk", re.compile(r"\bkuveyt\b(?: turk\w*)?(?: katilim\w*)?(?: bankasi\w*)?")),
    ("tom-katilim", re.compile(r"\b(?:tom|t o m) katilim\w*(?: bankasi\w*)?")),
    ("turkiye-finans", re.compile(r"\bturkiye finans\w*(?: katilim\w*)?(?: bankasi\w*)?")),
    ("vakif-katilim", re.compile(r"\bvakif\b(?: katilim\w*)?(?: bankasi\w*)?")),
    ("ziraat-katilim", re.compile(r"\bziraat\b(?: katilim\w*)?(?: bankasi\w*)?")),
)

_FINANCING_SUBFAMILY_TERMS = ("konut", "tasit", "ihtiyac")
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
    (
        QueryIntent.COMPARE,
        (
            "karsilastir",
            "kiyasla",
            "hangisi daha",
            "farklari",
            # Issue #16: superlative product questions ("en uygun oran hangi
            # bankada?") ask for a side-by-side reading of validated values.
            "en uygun",
            "en dusuk",
            "en yuksek",
            "en avantajli",
            "daha avantajli",
            "hangi banka",
            "hangisinde",
        ),
    ),
    (QueryIntent.GLOSSARY, ("ne demek", "sozluk")),
    (
        QueryIntent.DETAIL,
        (
            "detay",
            "ayrinti",
            "kosullari",
            # Issue #16: value interrogatives about one product ("vade kac
            # ay?", "oran ne kadar?") are detail lookups, not unknown intent.
            "kac ay",
            "kac tl",
            "ne kadar",
            "nedir",
            "yuzde kac",
        ),
    ),
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


def _campaign_type(
    text: str,
    family: ProductFamily | None,
    dimensions: list[ComparisonDimension],
) -> CampaignType | None:
    value = _first_match(text, _CAMPAIGN_TERMS)
    if (
        value in {CampaignType.PROFIT_SHARE, CampaignType.FINANCING_RATE}
        and family is ProductFamily.FINANCING
        and ComparisonDimension.RATE in dimensions
    ):
        # Issue #16: in a financing question, "kar payi orani" names the price
        # dimension being asked about, not a campaign-type filter; keeping the
        # inferred type would exclude the very records that state the rate.
        return None
    return value


def _bank_ids(text: str) -> list[str]:
    return [bank_id for bank_id, pattern in _BANK_PATTERNS if pattern.search(text)][:10]


def _consumed_spans(text: str) -> list[tuple[int, int]]:
    mappings = (_INTENT_TERMS, _PRODUCT_TERMS, _CAMPAIGN_TERMS, _DIMENSION_TERMS)
    spans = [
        match.span()
        for mapping in mappings
        for _, terms in mapping
        for term in terms
        for match in re.finditer(re.escape(term), text)
    ]
    spans.extend(
        match.span() for _, pattern in _BANK_PATTERNS for match in pattern.finditer(text)
    )
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

        family = _first_match(canonical, _PRODUCT_TERMS)
        dimensions = _dimensions(canonical)
        keywords = _keywords(canonical)
        if family is ProductFamily.FINANCING:
            # "tasit finansmani" scopes the question to one product sheet, not
            # just the financing family; the sub-family word re-enters the
            # keyword list (its span was consumed as a family term) so
            # relevance selection keeps konut/tasit/ihtiyac sheets apart.
            keywords = [
                *(
                    term
                    for term in _FINANCING_SUBFAMILY_TERMS
                    if term not in keywords and re.search(rf"\b{term}", canonical) is not None
                ),
                *keywords,
            ][:10]
        deterministic = ChatQueryPlan(
            intent=_intent(canonical),
            bank_ids=_bank_ids(canonical),
            product_family=family,
            campaign_type=_campaign_type(canonical, family, dimensions),
            comparison_dimensions=dimensions,
            keywords=keywords,
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
