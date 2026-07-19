"""Conservative Turkish rule extraction over evidence-addressable source blocks."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from katilim_analiz.contracts import (
    CampaignType,
    CleanDocument,
    EvidenceStatus,
    FeeBasis,
    FeeKind,
    FeeValue,
    MoneyValue,
    ProductFamily,
    RateKind,
    RatePeriod,
    RateValue,
    RewardBasis,
    RewardKind,
    RewardValue,
    SalesChannel,
    TermRange,
    ValidityWindow,
)
from katilim_analiz.domain import (
    normalize_money,
    normalize_rate,
    normalize_terms,
    normalize_validity,
)
from katilim_analiz.extraction.draft import BoundFact, CustomerSegmentFact, ExtractionDraft
from katilim_analiz.extraction.evidence import TextSpan, verify_document_blocks
from katilim_analiz.llm.contracts import ModelFactField
from katilim_analiz.llm.safety import is_obvious_prompt_injection

_RATE_MARKER = re.compile(r"%|‰|\byüzde\b|\byuzde\b|\bbinde\b|\bbaz\s+puan\b", re.I)
_MONEY_MARKER = re.compile(
    r"(?:₺|\bTL\b|\bTRY\b|\bTürk\s+lirası\b|\bUSD\b|\bEUR\b|\bdolar\b|\beuro\b)",
    re.I,
)
_MONEY_VALUE = re.compile(
    r"(?:₺\s*\d[\d.,]*|"
    r"\d[\d.,]*\s*(?:bin\s+|milyon\s+|milyar\s+)?"
    r"(?:TL|TRY|Türk\s+lirası|USD|EUR|dolar|euro))",
    re.I,
)
_DATE_MARKER = re.compile(
    r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|"
    r"\b(?:ocak|şubat|subat|mart|nisan|mayıs|mayis|haziran|temmuz|ağustos|agustos|"
    r"eylül|eylul|ekim|kasım|kasim|aralık|aralik)\b|bu\s+ay\s+sonuna",
    re.I,
)
# Turkish inflects the unit itself, as in "120 aya kadar", so a bare word
# boundary after the unit would hide the term from a normalizer that can already
# read it.  Suffixes are enumerated rather than left open so that an unrelated
# word sharing the stem, such as the "ayri" in "3 ayri urun", stays excluded.
_TERM_UNIT_SUFFIX = r"(?:[ae]|[dt][ae]|[dt][ae]n|[ıi]|[ıi]n|l[ıi][kğ][ıi]?|lar|ler)?"
_TERM_MARKER = re.compile(
    rf"\b(?:vade|vadeli|\d+\s*(?:ay|yıl|yil|sene){_TERM_UNIT_SUFFIX})\b",
    re.I,
)
# The stem carries case and derivation suffixes in Turkish, so the marker ends at
# the stem and lets the suffix run on; a trailing word boundary would miss both
# the possessive and the "-siz" derivation that states the fee does not apply.
_FEE_MARKER = re.compile(r"\b(?:ücret|ucret|masraf|tahsis|aidat)", re.I)
#: Phrases stating the fee is not charged, including the negated verb forms that
#: campaign pages actually use and the "covered by the bank" wording.
_FEE_WAIVER_MARKER = re.compile(
    r"\b(?:ücretsiz|ucretsiz|masrafsız|masrafsiz)\b"
    r"|\b(?:alınma|alinma|alınmıyor|alinmiyor|yansıtılma|yansitilma)\w*"
    r"|\b(?:tahsil\s+edilme|talep\s+edilme)\w*"
    r"|\b(?:banka\s+tarafından\s+karşılan|banka\s+tarafindan\s+karsilan)\w*"
    r"|\b(?:yoktur|bulunmamaktadır|bulunmamaktadir)\b",
    re.I,
)
#: A waiver that only holds up to a stated ceiling ("50.000 TL'ye kadar").
_FEE_WAIVER_LIMIT = re.compile(r"\bkadar\b", re.I)
_ELIGIBILITY_MARKER = re.compile(
    r"\b(?:yararlanabilir|yararlanmak|koşul|kosul|şart|sart|gerekmektedir|"
    r"zorunlu|yalnızca|yalnizca|sadece|üye\s+işyeri|uye\s+isyeri)\b",
    re.I,
)
_POSITIVE_SEGMENT_CONTEXT = re.compile(
    r"\b(?:yararlanabilir|yararlanmak|geçerli|gecerli|dahildir|kapsar|özel|ozel|"
    r"sunulur|yalnızca|yalnizca|sadece)\b",
    re.I,
)
_NEGATIVE_SEGMENT_CONTEXT = re.compile(
    r"\b(?:dahil\s+değildir|dahil\s+degildir|yararlanamaz|geçerli\s+değildir|"
    r"gecerli\s+degildir|kapsam\s+dışıdır|kapsam\s+disidir)\b",
    re.I,
)

_NEW_CUSTOMER_FORM = (
    r"(?:müşteri(?:lerimiz(?:e|in|den|de)?|ler(?:e|in|den|de)?|"
    r"miz(?:e|in|den|de)?|ye)?|"
    r"musteri(?:lerimiz(?:e|in|den|de)?|ler(?:e|in|den|de)?|"
    r"miz(?:e|in|den|de)?|ye)?)"
)
_NEW_CUSTOMER_TERM = rf"yeni\s+{_NEW_CUSTOMER_FORM}"
_NEW_CUSTOMER_PATTERN = re.compile(rf"\b{_NEW_CUSTOMER_TERM}\b", re.I)
_NEW_CUSTOMER_RESTRICTION_PATTERNS = (
    re.compile(
        rf"\b(?P<restriction>(?:yalnızca|yalnizca|sadece)\s+"
        rf"(?P<segment>{_NEW_CUSTOMER_TERM}))\b",
        re.I,
    ),
    re.compile(
        rf"\b(?P<restriction>(?P<segment>{_NEW_CUSTOMER_TERM})\s+"
        rf"(?:için|icin)\s+(?:geçerli|gecerli|özel|ozel))\b",
        re.I,
    ),
    re.compile(
        rf"\b(?P<restriction>(?P<segment>{_NEW_CUSTOMER_TERM})\s+"
        rf"(?:özel|ozel|(?:kampanyadan\s+)?(?:yararlanabilir|faydalanabilir)))\b",
        re.I,
    ),
)

_PRODUCT_PATTERNS: Mapping[ProductFamily, tuple[re.Pattern[str], ...]] = {
    ProductFamily.FINANCING: (
        re.compile(
            r"\b(?:finansman|konut\s+finansmanı|taşıt\s+finansmanı|ihtiyaç\s+finansmanı)\b", re.I
        ),
    ),
    ProductFamily.CARD: (
        re.compile(r"\b(?:kredi\s+kartı|banka\s+kartı|kartınız|kartiniz|kart)\b", re.I),
    ),
    ProductFamily.PARTICIPATION_ACCOUNT: (
        re.compile(
            r"\b(?:katılma\s+hesabı|katilim\s+hesabi|kâr\s+payı\s+dağıtım|kar\s+payi\s+dagitim)\b",
            re.I,
        ),
    ),
    ProductFamily.INVESTMENT: (
        re.compile(r"\b(?:yatırım|yatirim|altın\s+hesabı|altin\s+hesabi|yatırım\s+fonu)\b", re.I),
    ),
}

_CAMPAIGN_PATTERNS: Mapping[CampaignType, tuple[re.Pattern[str], ...]] = {
    CampaignType.CASHBACK: (re.compile(r"\b(?:nakit\s+iade|para\s+iadesi)\b", re.I),),
    CampaignType.DISCOUNT: (re.compile(r"\bindirim(?:li|i)?\b", re.I),),
    CampaignType.POINTS: (re.compile(r"\b(?:puan|bonus)\b", re.I),),
    CampaignType.FEE_WAIVER: (
        re.compile(r"\b(?:masrafsız|masrafsiz|ücretsiz|ucretsiz|ücret\s+alınmayacak)\b", re.I),
    ),
    CampaignType.WELCOME: (
        re.compile(r"\b(?:hoş\s+geldin|hos\s+geldin)\b", re.I),
        *_NEW_CUSTOMER_RESTRICTION_PATTERNS,
    ),
    CampaignType.INSTALLMENT: (re.compile(r"\btaksit(?:li|lendirme)?\b", re.I),),
    CampaignType.FINANCING_RATE: (
        re.compile(r"\bfinansman(?:\s+kâr|\s+kar)?\s+(?:payı\s+)?oranı\b", re.I),
    ),
    CampaignType.PROFIT_SHARE: (
        re.compile(
            r"\b(?:kâr\s+payı\s+dağıtım|kar\s+payi\s+dagitim|kâr\s+paylaşım|kar\s+paylasim)\s+oranı\b",
            re.I,
        ),
    ),
}

_SEGMENT_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "bireysel": re.compile(r"\bbireysel\b", re.I),
    "ticari": re.compile(r"\bticari\b", re.I),
    "kobi": re.compile(r"\bKOBİ\b|\bkobi\b", re.I),
    "emekli": re.compile(r"\bemekli(?:ler)?\b", re.I),
    "genc": re.compile(r"\bgenç(?:ler)?\b|\bgenc(?:ler)?\b", re.I),
    "kadin_girisimci": re.compile(r"\bkadın\s+girişimci\b|\bkadin\s+girisimci\b", re.I),
    "yeni_musteri": _NEW_CUSTOMER_PATTERN,
}

_COMBINED_DIGITAL_CHANNEL_PATTERN = re.compile(
    r"\b(?:Albaraka\s+)?Mobil\s+(?:ve|/)\s+İnternet"
    r"(?:\s+(?:Bankacılığı|Bankaciligi))?(?:\s+kanalları)?\s+(?:üzerinden|uzerinden)\b",
    re.I,
)
_CHANNEL_PATTERNS: Mapping[SalesChannel, re.Pattern[str]] = {
    SalesChannel.MOBILE: re.compile(
        r"\b(?:mobil\s+(?:şube(?:den)?|sube(?:den)?|uygulama(?:dan)?)|Albaraka\s+Mobil)\b",
        re.I,
    ),
    SalesChannel.WEB: re.compile(
        r"\b(?:internet\s+(?:şubesi|subesi|bankacılığı|bankaciligi|üzerinden|uzerinden)|"
        r"web\s+sitesi)\b",
        re.I,
    ),
    SalesChannel.BRANCH: re.compile(
        r"(?<!mobil\s)(?<!internet\s)\b(?:şube|sube)"
        r"(?:ye|de|den|ler(?:e|de|den)?)?\b",
        re.I,
    ),
    SalesChannel.DIGITAL: re.compile(r"\bdijital\s+kanal(?:lar)?\b", re.I),
}
_POSITIVE_CHANNEL_CONTEXT = re.compile(
    r"\b(?:başvur\w*|basvur\w*|üzerinden|uzerinden|kanal\w*|geçerli|gecerli|"
    r"yapılabilir|yapilabilir|tamamlan\w*|sunul\w*|kullanılabilir|kullanilabilir|"
    r"talep\w*)\b",
    re.I,
)
_NEGATIVE_CHANNEL_CONTEXT = re.compile(
    r"\b(?:yararlanamaz|faydalanamaz|geçerli\s+değildir|gecerli\s+degildir|"
    r"başvur(?:u\s+)?yapılamaz|basvur(?:u\s+)?yapilamaz|başvuramaz|basvuramaz|"
    r"kullanılamaz|kullanilamaz|hariç|haric)\b",
    re.I,
)
_EXCLUSIVE_CHANNEL_CONTEXT = re.compile(r"\b(?:yalnızca|yalnizca|sadece)\b", re.I)

_GENERIC_PROFIT_RATE_LABEL = re.compile(
    r"\b(?:aylık\s+|aylik\s+)?(?:kâr|kar)\s+(?:payı\s+|payi\s+)?oranı\b",
    re.I,
)
_FINANCING_RATE_CONTEXT = re.compile(r"\b(?:finansman|kullandırım|kullandirim)\b", re.I)
_NON_FINANCING_PROFIT_CONTEXT = re.compile(
    r"\b(?:katılma\s+hesabı|katilim\s+hesabi|dağıtım|dagitim|paylaşım|paylasim|"
    r"geçmiş\s+getiri|gecmis\s+getiri)\b",
    re.I,
)

_KARZ_I_HASEN_TERM = r"karz\s*[-‐‑‒–—]\s*[ıi]\s+hasen"
_PRODUCT_MECHANISM_CONTEXT_PATTERNS: Mapping[str, tuple[re.Pattern[str], ...]] = {
    "karz_i_hasen": (
        re.compile(
            rf"\b(?:finansman|kart|ürün|urun|hesap)[^.!?;\n]{{0,80}}"
            rf"\(\s*(?P<mechanism>{_KARZ_I_HASEN_TERM})\s*\)",
            re.I,
        ),
        re.compile(
            rf"\b(?:finansman|ürün|urun)\s+"
            rf"(?:yöntemi|yontemi|modeli|mekanizması|mekanizmasi)\s*(?:olarak|:)?\s*"
            rf"(?P<mechanism>{_KARZ_I_HASEN_TERM})\b",
            re.I,
        ),
    ),
}


def _sentences(text: str, block_id: str) -> tuple[TextSpan, ...]:
    spans: list[TextSpan] = []
    boundaries = tuple(re.finditer(r"[!?;](?=\s|$)|\.(?=\s|$)|\n", text))
    raw_spans: list[tuple[int, int]] = []
    cursor = 0
    for boundary in boundaries:
        end = boundary.start() if boundary.group() == "\n" else boundary.end()
        raw_spans.append((cursor, end))
        cursor = boundary.end()
    raw_spans.append((cursor, len(text)))
    for raw_start, raw_end in raw_spans:
        raw = text[raw_start:raw_end]
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        quote = raw[left:right]
        if not quote or len(quote) > 500:
            continue
        start = raw_start + left
        spans.append(TextSpan(block_id, quote, start, start + len(quote)))
    return tuple(spans)


def _money_value_spans(sentence: TextSpan) -> tuple[TextSpan, ...]:
    return tuple(
        TextSpan(
            sentence.block_id,
            match.group(),
            sentence.start_char + match.start(),
            sentence.start_char + match.end(),
        )
        for match in _MONEY_VALUE.finditer(sentence.quote)
        if len(match.group()) <= 500
    )


def _pattern_hits[T: StrEnum](
    blocks: Iterable[tuple[str, str]],
    patterns: Mapping[T, tuple[re.Pattern[str], ...]],
) -> dict[T, TextSpan]:
    hits: dict[T, TextSpan] = {}
    for block_id, text in blocks:
        for value, value_patterns in patterns.items():
            if value in hits:
                continue
            for pattern in value_patterns:
                match = pattern.search(text)
                if match is not None and 0 < len(match.group()) <= 500:
                    hits[value] = TextSpan(block_id, match.group(), match.start(), match.end())
                    break
    return hits


def _new_customer_restriction_match(text: str) -> re.Match[str] | None:
    for pattern in _NEW_CUSTOMER_RESTRICTION_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return match
    return None


def is_explicit_new_customer_restriction(text: str) -> bool:
    """Return whether text explicitly limits eligibility to new customers."""

    return _new_customer_restriction_match(text) is not None


def _channel_matches(text: str) -> tuple[tuple[SalesChannel, re.Match[str]], ...]:
    if (
        _POSITIVE_CHANNEL_CONTEXT.search(text) is None
        or _NEGATIVE_CHANNEL_CONTEXT.search(text) is not None
    ):
        return ()

    combined_matches = tuple(_COMBINED_DIGITAL_CHANNEL_PATTERN.finditer(text))
    matches: list[tuple[SalesChannel, re.Match[str]]] = [
        (SalesChannel.DIGITAL, match) for match in combined_matches
    ]
    combined_spans = tuple(match.span() for match in combined_matches)
    for channel, pattern in _CHANNEL_PATTERNS.items():
        for match in pattern.finditer(text):
            if any(
                match.start() < combined_end and combined_start < match.end()
                for combined_start, combined_end in combined_spans
            ):
                continue
            matches.append((channel, match))
    return tuple(matches)


def _channel_priority(text: str) -> int:
    return 2 if _EXCLUSIVE_CHANNEL_CONTEXT.search(text) is not None else 1


def _rate_context_hints(
    blocks: Iterable[tuple[str, str, str]],
    family_hits: Mapping[ProductFamily, TextSpan],
) -> dict[str, tuple[RateKind, RatePeriod | None]]:
    hints: dict[str, tuple[RateKind, RatePeriod | None]] = {}
    document_is_unambiguously_financing = set(family_hits) == {ProductFamily.FINANCING}
    current_table_hint: tuple[RateKind, RatePeriod | None] | None = None

    for block_id, kind, text in blocks:
        label = _GENERIC_PROFIT_RATE_LABEL.search(text)
        if kind != "table":
            current_table_hint = None

        if label is not None:
            is_financing = (
                document_is_unambiguously_financing
                or _FINANCING_RATE_CONTEXT.search(text) is not None
            )
            has_conflict = _NON_FINANCING_PROFIT_CONTEXT.search(text) is not None
            hint = (
                (
                    RateKind.FINANCING_PROFIT_RATE,
                    (
                        RatePeriod.MONTHLY
                        if re.search(r"\b(?:aylık|aylik)\b", label.group(), re.I)
                        else None
                    ),
                )
                if is_financing and not has_conflict
                else None
            )
            if kind == "table":
                current_table_hint = hint
            if hint is not None:
                hints[block_id] = hint
            continue

        if kind == "table" and current_table_hint is not None:
            hints[block_id] = current_table_hint

    return hints


def _product_mechanism_hit(
    blocks: Iterable[tuple[str, str]],
) -> tuple[str, TextSpan] | None:
    hits: dict[str, TextSpan] = {}
    for block_id, text in blocks:
        for mechanism, patterns in _PRODUCT_MECHANISM_CONTEXT_PATTERNS.items():
            if mechanism in hits:
                continue
            for pattern in patterns:
                match = pattern.search(text)
                if match is None:
                    continue
                start, end = match.span("mechanism")
                hits[mechanism] = TextSpan(block_id, text[start:end], start, end)
                break
    if len(hits) != 1:
        return None
    return next(iter(hits.items()))


def supported_product_families(text: str) -> frozenset[ProductFamily]:
    return frozenset(
        value
        for value, patterns in _PRODUCT_PATTERNS.items()
        if any(pattern.search(text) is not None for pattern in patterns)
    )


def supported_campaign_types(text: str) -> frozenset[CampaignType]:
    return frozenset(
        value
        for value, patterns in _CAMPAIGN_PATTERNS.items()
        if any(pattern.search(text) is not None for pattern in patterns)
    )


def supported_sales_channels(text: str) -> frozenset[SalesChannel]:
    channels: set[SalesChannel] = set()
    for sentence in _sentences(text, "model-evidence"):
        channels.update(channel for channel, _match in _channel_matches(sentence.quote))
    return frozenset(channels)


def segment_key_for_text(text: str) -> str | None:
    matches = [key for key, pattern in _SEGMENT_PATTERNS.items() if pattern.search(text)]
    return matches[0] if len(matches) == 1 else None


def is_explicit_eligibility(text: str) -> bool:
    return _ELIGIBILITY_MARKER.search(text) is not None


def _is_eligible_segment_context(text: str, key: str) -> bool:
    if key == "yeni_musteri":
        return is_explicit_new_customer_restriction(text)
    if _NEGATIVE_SEGMENT_CONTEXT.search(text) is not None:
        return False
    return _POSITIVE_SEGMENT_CONTEXT.search(text) is not None


def _dedupe_bound[T](facts: Iterable[BoundFact[T]]) -> tuple[BoundFact[T], ...]:
    seen: set[tuple[str, int, int, str]] = set()
    result: list[BoundFact[T]] = []
    for fact in facts:
        key = (fact.span.block_id, fact.span.start_char, fact.span.end_char, repr(fact.value))
        if key not in seen:
            seen.add(key)
            result.append(fact)
    return tuple(result)


def _fee_from_span(span: TextSpan) -> FeeValue | None:
    text = span.quote
    lowered = text.casefold()
    if _FEE_MARKER.search(lowered) is None:
        return None
    kind = FeeKind.OTHER
    basis = FeeBasis.ONE_TIME
    if "tahsis" in lowered:
        kind = FeeKind.ALLOCATION
    elif "aidat" in lowered or "yıllık" in lowered or "yillik" in lowered:
        kind = FeeKind.ANNUAL
        basis = FeeBasis.PER_YEAR
    elif "aylık" in lowered or "aylik" in lowered:
        kind = FeeKind.MONTHLY
        basis = FeeBasis.PER_MONTH
    elif "işlem" in lowered or "islem" in lowered:
        kind = FeeKind.TRANSACTION
        basis = FeeBasis.PER_TRANSACTION

    money = normalize_money(text)
    # A waiver is decided before any amount is read.  A sentence waiving the fee
    # up to 50.000 TL carries a figure that bounds the waiver, and reading that
    # figure as the fee would publish the exact opposite of what the source says.
    if _FEE_WAIVER_MARKER.search(lowered) is not None:
        return FeeValue(
            raw=text,
            kind=kind,
            basis=basis,
            description=text,
            waived=True,
            waiver_limit=money.value if _FEE_WAIVER_LIMIT.search(lowered) is not None else None,
        )
    if money.value is not None:
        return FeeValue(raw=text, money=money.value, kind=kind, basis=basis)
    rate = normalize_rate(text)
    if rate.value is not None:
        return FeeValue(
            raw=text,
            rate=rate.value,
            kind=kind,
            basis=FeeBasis.PERCENT_OF_AMOUNT,
        )
    return None


def _reward_from_span(span: TextSpan) -> RewardValue | None:
    text = span.quote
    lowered = text.casefold()
    points_matches = tuple(re.finditer(r"\b(\d+(?:[.,]\d+)?)\s*puan\b", lowered))
    if len(points_matches) == 1:
        try:
            points = Decimal(points_matches[0].group(1).replace(",", "."))
        except InvalidOperation:
            return None
        return RewardValue(
            raw=text,
            kind=RewardKind.POINTS,
            basis=RewardBasis.CAMPAIGN_TOTAL,
            points=points,
        )
    if re.search(r"\b(?:nakit\s+iade|para\s+iadesi)\b", lowered):
        money = normalize_money(text)
        if money.value is not None:
            return RewardValue(
                raw=text,
                kind=RewardKind.MONEY,
                basis=RewardBasis.CAMPAIGN_TOTAL,
                money=money.value,
            )
    if "indirim" in lowered:
        rate = normalize_rate(text, kind_hint=RateKind.DISCOUNT_RATE)
        if rate.value is not None:
            return RewardValue(
                raw=text,
                kind=RewardKind.DISCOUNT,
                basis=RewardBasis.PER_TRANSACTION,
                rate=rate.value,
            )
        money = normalize_money(text)
        if money.value is not None:
            return RewardValue(
                raw=text,
                kind=RewardKind.DISCOUNT,
                basis=RewardBasis.PER_TRANSACTION,
                money=money.value,
            )
    return None


def extract_rules(document: CleanDocument) -> ExtractionDraft:
    """Extract only exact, locally normalizable facts and report unresolved fields."""

    verify_document_blocks(document)
    safe_source_blocks = tuple(
        block for block in document.blocks if not is_obvious_prompt_injection(block.text)
    )
    safe_blocks = tuple((block.id, block.text) for block in safe_source_blocks)
    quarantined = tuple(
        block.id for block in document.blocks if is_obvious_prompt_injection(block.text)
    )
    issues = [f"quarantined_prompt_injection_block:{block_id}" for block_id in quarantined]

    title: BoundFact[str] | None = None
    for block in document.blocks:
        if (
            block.kind == "heading"
            and not is_obvious_prompt_injection(block.text)
            and len(block.text) <= 500
        ):
            span = TextSpan(block.id, block.text, 0, len(block.text))
            title = BoundFact(block.text, span)
            break

    family_hits = _pattern_hits(safe_blocks, _PRODUCT_PATTERNS)
    product_family: BoundFact[ProductFamily] | None = None
    if len(family_hits) == 1:
        family, span = next(iter(family_hits.items()))
        product_family = BoundFact(family, span, inferred=True)
    elif len(family_hits) > 1:
        issues.append("product_family_ambiguous")

    type_hits = _pattern_hits(safe_blocks, _CAMPAIGN_PATTERNS)
    campaign_type: BoundFact[CampaignType] | None = None
    if len(type_hits) == 1:
        campaign_value, span = next(iter(type_hits.items()))
        campaign_type = BoundFact(campaign_value, span, inferred=True)
    elif len(type_hits) > 1:
        issues.append("campaign_type_ambiguous")

    rate_context_hints = _rate_context_hints(
        ((block.id, block.kind, block.text) for block in safe_source_blocks),
        family_hits,
    )

    all_sentences = tuple(
        sentence for block_id, text in safe_blocks for sentence in _sentences(text, block_id)
    )
    rates: list[BoundFact[RateValue]] = []
    amounts: list[BoundFact[MoneyValue]] = []
    terms: list[BoundFact[TermRange]] = []
    fees: list[BoundFact[FeeValue]] = []
    rewards: list[BoundFact[RewardValue]] = []
    validity: BoundFact[ValidityWindow] | None = None
    conditions: list[BoundFact[str]] = []

    for span in all_sentences:
        text = span.quote
        lowered = text.casefold()
        if _RATE_MARKER.search(text) is not None:
            normalized_rate = normalize_rate(text)
            context_hint = rate_context_hints.get(span.block_id)
            if (
                context_hint is not None
                and normalized_rate.value is not None
                and normalized_rate.value.kind is RateKind.UNKNOWN
            ):
                kind_hint, period_hint = context_hint
                normalized_rate = normalize_rate(
                    text,
                    kind_hint=kind_hint,
                    period_hint=period_hint,
                )
            if normalized_rate.value is not None:
                rates.append(
                    BoundFact(
                        normalized_rate.value,
                        span,
                        inferred=normalized_rate.value.status is EvidenceStatus.INFERRED,
                    )
                )
        if _MONEY_MARKER.search(text) is not None:
            is_financing_context = re.search(
                r"\b(?:finansman|kullandırım|kullandirim|tutar)\b", lowered
            )
            is_fee_or_reward = re.search(
                r"\b(?:ücret|ucret|masraf|tahsis|aidat|iade|indirim|puan)\b", lowered
            )
            if is_financing_context is not None and is_fee_or_reward is None:
                money_spans = _money_value_spans(span)
                if len(money_spans) == 1:
                    money_span = money_spans[0]
                    normalized_money = normalize_money(money_span.quote)
                    if normalized_money.value is not None:
                        amounts.append(
                            BoundFact(
                                normalized_money.value,
                                money_span,
                                inferred=(normalized_money.value.status is EvidenceStatus.INFERRED),
                            )
                        )
        if _TERM_MARKER.search(text) is not None:
            normalized_terms = normalize_terms(text)
            if normalized_terms.value is not None:
                terms.extend(BoundFact(value, span) for value in normalized_terms.value)
        if validity is None and _DATE_MARKER.search(text) is not None:
            normalized_validity = normalize_validity(
                text,
                reference_date=document.cleaned_at.date(),
            )
            if normalized_validity.value is not None:
                validity = BoundFact(
                    normalized_validity.value,
                    span,
                    inferred=normalized_validity.value.status is EvidenceStatus.INFERRED,
                )
        fee = _fee_from_span(span)
        if fee is not None:
            fees.append(BoundFact(fee, span))
        reward = _reward_from_span(span)
        if reward is not None:
            rewards.append(BoundFact(reward, span))
        if _ELIGIBILITY_MARKER.search(text) is not None:
            conditions.append(BoundFact(text, span))

    segments: list[CustomerSegmentFact] = []
    for block_id, text in safe_blocks:
        for key, pattern in _SEGMENT_PATTERNS.items():
            restriction_match = (
                _new_customer_restriction_match(text) if key == "yeni_musteri" else None
            )
            match = restriction_match if restriction_match is not None else pattern.search(text)
            if match is not None and _is_eligible_segment_context(text, key):
                if key == "yeni_musteri":
                    start, end = match.span("segment")
                else:
                    start, end = match.span()
                span = TextSpan(block_id, text[start:end], start, end)
                if key not in {item.canonical_key for item in segments}:
                    segments.append(CustomerSegmentFact(span.quote, key, span))

    channel_hits: dict[SalesChannel, TextSpan] = {}
    channel_priority = 0
    for sentence in all_sentences:
        matches = _channel_matches(sentence.quote)
        if not matches:
            continue
        sentence_priority = _channel_priority(sentence.quote)
        if sentence_priority < channel_priority:
            continue
        if sentence_priority > channel_priority:
            channel_hits.clear()
            channel_priority = sentence_priority
        for channel, match in matches:
            if channel not in channel_hits:
                if _POSITIVE_CHANNEL_CONTEXT.search(match.group()) is not None:
                    start = sentence.start_char + match.start()
                    end = sentence.start_char + match.end()
                    channel_hits[channel] = TextSpan(
                        sentence.block_id,
                        match.group(),
                        start,
                        end,
                    )
                else:
                    channel_hits[channel] = sentence
    sales_channel: BoundFact[SalesChannel] | None = None
    if len(channel_hits) == 1:
        channel, span = next(iter(channel_hits.items()))
        sales_channel = BoundFact(channel, span, inferred=True)
    elif len(channel_hits) > 1:
        issues.append("sales_channel_ambiguous")

    new_customer_only: BoundFact[bool] | None = None
    for block_id, text in safe_blocks:
        match = _new_customer_restriction_match(text)
        if match is not None:
            start, end = match.span("restriction")
            new_customer_only = BoundFact(
                True,
                TextSpan(block_id, text[start:end], start, end),
                inferred=True,
            )
            break

    product_mechanism: BoundFact[str] | None = None
    mechanism_hit = _product_mechanism_hit(safe_blocks)
    if mechanism_hit is not None:
        mechanism, span = mechanism_hit
        product_mechanism = BoundFact(mechanism, span, inferred=True)

    unresolved: set[ModelFactField] = set()
    if title is None:
        unresolved.add(ModelFactField.TITLE)
    if product_family is None:
        unresolved.add(ModelFactField.PRODUCT_FAMILY)
    if campaign_type is None:
        unresolved.add(ModelFactField.CAMPAIGN_TYPE)
    typed_rates = _dedupe_bound(rates)
    if not typed_rates or any(
        rate.value.kind is RateKind.UNKNOWN or rate.value.period is RatePeriod.UNSPECIFIED
        for rate in typed_rates
    ):
        unresolved.add(ModelFactField.RATE)
    typed_amounts = _dedupe_bound(amounts)
    if not typed_amounts:
        unresolved.add(ModelFactField.FINANCING_AMOUNT)
    typed_terms = _dedupe_bound(terms)
    if not typed_terms:
        unresolved.add(ModelFactField.TERM)
    typed_fees = _dedupe_bound(fees)
    if not typed_fees:
        unresolved.add(ModelFactField.FEE)
    typed_rewards = _dedupe_bound(rewards)
    if not typed_rewards:
        unresolved.add(ModelFactField.REWARD)
    if validity is None:
        unresolved.add(ModelFactField.VALIDITY)
    if not segments:
        unresolved.add(ModelFactField.CUSTOMER_SEGMENT)
    typed_conditions = _dedupe_bound(conditions)
    if not typed_conditions:
        unresolved.add(ModelFactField.ELIGIBILITY_CONDITION)
    if sales_channel is None:
        unresolved.add(ModelFactField.SALES_CHANNEL)
    if new_customer_only is None:
        unresolved.add(ModelFactField.NEW_CUSTOMER_ONLY)

    return ExtractionDraft(
        bank_id=document.bank_id,
        title=title,
        product_family=product_family,
        campaign_type=campaign_type,
        rates=typed_rates,
        financing_amounts=typed_amounts,
        terms=typed_terms,
        fees=typed_fees,
        rewards=typed_rewards,
        validity=validity,
        customer_segments=tuple(segments),
        eligibility_conditions=typed_conditions,
        sales_channel=sales_channel,
        new_customer_only=new_customer_only,
        product_mechanism=product_mechanism,
        issues=tuple(issues),
        unresolved_fields=frozenset(unresolved),
    )


def with_issue(draft: ExtractionDraft, issue: str) -> ExtractionDraft:
    if issue in draft.issues:
        return draft
    return replace(draft, issues=(*draft.issues, issue))


__all__ = [
    "extract_rules",
    "is_explicit_eligibility",
    "is_explicit_new_customer_restriction",
    "segment_key_for_text",
    "supported_campaign_types",
    "supported_product_families",
    "supported_sales_channels",
    "with_issue",
]
