"""Conservative Turkish rule extraction over evidence-addressable source blocks."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from katilim_analiz.contracts import (
    CampaignType,
    CleanDocument,
    EvidenceStatus,
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
from katilim_analiz.extraction.fees import read_fee
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
#: Evidence-based currency binding: a TL amount is a lira figure the page
#: itself states (``500.000 TL``, ``₺250``, ``1.000 Turk lirasi``).  The match
#: is the exact grounding quote for an inferred ``product_currency``.
_TRY_AMOUNT = re.compile(
    r"(?:₺\s*\d[\d.,]*|"
    r"\d[\d.,]*\s*(?:bin\s+|milyon\s+|milyar\s+)?(?:TL|TRY|Türk\s+lirası)\b)",
    re.I,
)
#: Any non-lira currency symbol or code anywhere on the page is a mixed-signal
#: veto: the mere mention of USD/EUR pricing ("USD/EUR azami 60 ay") means the
#: product is not single-currency, so the binding abstains.
_FOREIGN_CURRENCY_SIGNAL = re.compile(
    r"[$€£]|\b(?:USD|EUR|GBP|CHF|JPY|SAR|AED|RUB|CNY)\b"
    r"|\b(?:dolar|euro|avro|sterlin|frang|frank)\w*",
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
#: Vouchers and gift cards carry a stated lira value and are named as a phrase,
#: because the bare stem also begins unrelated words such as the raffle below.
_VOUCHER_MARKER = re.compile(
    r"\b(?:alışveriş|alisveris|hediye)\s+(?:çeki|ceki|kartı|karti)\b"
    r"|\bhediye\s+(?:çek|cek)\w*",
    re.I,
)
#: A draw is a chance at a prize, not a reward the campaign grants, so its
#: prize figure must never be recorded as an amount the customer receives.
_RAFFLE_MARKER = re.compile(r"\b(?:çekiliş|cekilis|kura)\w*", re.I)
_ELIGIBILITY_MARKER = re.compile(
    r"\b(?:yararlanabilir|yararlanmak|koşul|kosul|şart|sart|gerekmektedir|"
    r"zorunlu|yalnızca|yalnizca|sadece|üye\s+işyeri|uye\s+isyeri)\b",
    re.I,
)
_POSITIVE_SEGMENT_CONTEXT = re.compile(
    r"\b(?:yararlanabilir|yararlanmak|geçerli|gecerli|dahildir|kapsar|özel|ozel|"
    r"sunulur|yalnızca|yalnizca|sadece|"
    # "saglanacak"/"saglanan" and "yonelik" scope a product to an audience the
    # same way "sunulur" does ("gercek kisi musterilerimize saglanacak bireysel
    # bir finansman"); both are affirmative scoping forms, never exclusions.
    r"sağlan\w*|saglan\w*|yönelik|yonelik)\b",
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

#: Evidence-based universal eligibility: a page that explicitly opens the
#: campaign to every customer ("tüm müşterilerimiz", "mevcut ve yeni
#: müşteriler") states that no new-customer restriction exists, which grounds
#: ``new_customer_only=False`` to that exact quote.  Pure absence of any
#: signal never binds a value (ADR-002).  The plural/possessive customer form
#: is required so an unrelated noun phrase ("tüm müşteri temsilcileri") does
#: not read as an eligibility statement.
_UNIVERSAL_CUSTOMER_FORM = (
    r"(?:müşteriler(?:imiz)?(?:e|in|den|de)?|musteriler(?:imiz)?(?:e|in|den|de)?)"
)
_NEW_CUSTOMER_UNIVERSAL_PATTERNS = (
    re.compile(
        rf"\b(?P<universal>(?:t[üu]m|b[üu]t[üu]n)\s+{_UNIVERSAL_CUSTOMER_FORM})\b",
        re.I,
    ),
    re.compile(
        rf"\b(?P<universal>mevcut\s+ve\s+{_NEW_CUSTOMER_TERM})\b",
        re.I,
    ),
    re.compile(
        rf"\b(?P<universal>yeni\s+ve\s+mevcut\s+{_UNIVERSAL_CUSTOMER_FORM})\b",
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
        # A bank's card-programme brand names the card family deterministically
        # even when the page never says "kart": Emlak Katilim's Paraf campaign
        # pages ("Paraf ile ... taksit", "... TL ParafPara") carry no other
        # family marker at all. The brand forms are enumerated exactly; the
        # bare stem would also match "paraflamak" (to initial a document).
        re.compile(r"\bparaf(?:para|kart)?\b", re.I),
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
    # "Gercek kisi" is banking Turkish for a natural person; a page that scopes
    # eligibility to "gercek kisi (bireysel)" states the retail segment without
    # necessarily using the word "bireysel" itself (Vakif Katilim financing
    # pages), so both wordings bind the same canonical key.
    "bireysel": re.compile(
        r"\bbireysel\b|\bger[cç]ek\s+ki[şs][iı](?:ler)?\b",
        re.I,
    ),
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
#: Issue #28: a "Finansman Orani" column in a value-bracket table
#: (tutar araligi | % | vade) states the financing share of the asset value
#: (LTV), not a profit rate.  The label alone is not enough — the bracket
#: context must also be present, otherwise no hint is asserted.
_LTV_TABLE_LABEL = re.compile(r"\b(?:azami\s+)?finansman\s+oran", re.I)
_AMOUNT_BRACKET_CONTEXT = re.compile(r"\btutar\b|\baral[ıi][gğk]", re.I)
#: Issue #28: a percentage in a fee sentence ("tahsis ucreti ... %0,5",
#: "komisyon %X") prices a fee, never a financing rate.  The fee reader owns
#: that sentence; recording its percentage as a rate mislabels a cost as a
#: price, so the whole sentence is excluded from rate extraction.
_FEE_PERCENT_CONTEXT = re.compile(
    r"\b(?:ücret|ucret|masraf|tahsis|aidat|komisyon)",
    re.I,
)
_FINANCING_RATE_CONTEXT = re.compile(r"\b(?:finansman|kullandırım|kullandirim)\b", re.I)
#: Issue #30: a financing installment table names its profit column only in the
#: header ("Kar Orani" / "Kar Payi Orani" variants) while the rows are bare
#: value cells ("30.000,00 TL | 12 Ay | 1,69%").  The cleaner joins each row's
#: cells with " | ", so the header states which column carries the profit rate
#: and the rows can be read positionally.  A column whose own label says
#: "yillik" or "maliyet" is a cost, never the profit column.
_PROFIT_RATE_COLUMN_LABEL = re.compile(
    r"\b(?:aylık\s+|aylik\s+)?k[âa]r\s+(?:pay[ıi]\s+)?oran[ıi]?\b",
    re.I,
)
_ANNUAL_OR_COST_COLUMN_LABEL = re.compile(r"\b(?:yıllık|yillik|senelik)\b|maliyet", re.I)
_TERM_COLUMN_LABEL = re.compile(r"\b(?:vade|ay)\b", re.I)
#: Any header cell that names a percentage of its own; used to decide whether a
#: ragged row's single percent can still be attributed to the profit column.
_RATE_LIKE_COLUMN_LABEL = re.compile(r"oran|%|maliyet|getiri", re.I)
_TABLE_CELL_SEPARATOR = "|"
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


def _new_customer_universal_match(text: str) -> re.Match[str] | None:
    for pattern in _NEW_CUSTOMER_UNIVERSAL_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return match
    return None


def is_explicit_new_customer_universal(text: str) -> bool:
    """Return whether text explicitly opens eligibility to all customers."""

    return _new_customer_universal_match(text) is not None


def is_single_currency_try_quote(text: str) -> bool:
    """Return whether one quote alone carries a TL amount and no foreign signal."""

    return _TRY_AMOUNT.search(text) is not None and _FOREIGN_CURRENCY_SIGNAL.search(text) is None


def infer_page_currency(document: CleanDocument) -> TextSpan | None:
    """Ground ``product_currency=TRY`` in single-currency TL page evidence.

    The rule is deliberately page-scoped and vetoed by any foreign signal: TRY
    is bound only when the document's safe (non-quarantined) blocks state at
    least one lira amount AND no block anywhere mentions another currency
    symbol or code.  A page that also prices in USD/EUR ("TL cinsinden 120 aya
    kadar; USD/EUR azami 60 ay") therefore stays unknown — this is
    evidence-based inference grounded to an exact TL-amount quote, never a
    default (ADR-002).  The record's own fact quotes are part of that scope by
    construction; scanning the whole page instead of only the fact quotes can
    only widen the veto, so it is strictly more conservative.

    Returns the span of the first TL amount as the grounding quote, or ``None``
    when currency signals are absent or mixed.
    """

    first_try_span: TextSpan | None = None
    for block in document.blocks:
        if is_obvious_prompt_injection(block.text):
            continue
        if _FOREIGN_CURRENCY_SIGNAL.search(block.text) is not None:
            return None
        if first_try_span is None:
            match = _TRY_AMOUNT.search(block.text)
            if match is not None and 0 < len(match.group()) <= 500:
                first_try_span = TextSpan(block.id, match.group(), match.start(), match.end())
    return first_try_span


def rate_semantics_unresolved(rates: Iterable[RateValue]) -> bool:
    """Return whether the pricing-rate question is still open for these rates.

    Issue #28: an LTV share is a structural ceiling, not a price.  It neither
    answers the rate question nor blocks it — it has no period by nature, so
    demanding one would keep every record with an LTV table permanently
    unresolved.
    """

    pricing = tuple(rate for rate in rates if rate.kind is not RateKind.LTV_RATIO)
    return not pricing or any(
        rate.kind is RateKind.UNKNOWN or rate.period is RatePeriod.UNSPECIFIED for rate in pricing
    )


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


@dataclass(frozen=True)
class _RateContextHint:
    """A rate meaning established by surrounding context, usually a table header.

    When ``column_count`` is set the hint came from a header whose cells could
    be read positionally; the hint then applies only to the matching column of
    each row, never to the row as a whole.
    """

    kind: RateKind
    period: RatePeriod | None
    profit_column: int | None = None
    column_count: int | None = None
    sole_rate_column: bool = False


def _table_cells(text: str) -> tuple[tuple[int, int, str], ...]:
    """Split one serialized table row into (start, end, stripped_cell) triples."""

    cells: list[tuple[int, int, str]] = []
    cursor = 0
    for part in text.split(_TABLE_CELL_SEPARATOR):
        start = cursor + (len(part) - len(part.lstrip()))
        stripped = part.strip()
        cells.append((start, start + len(stripped), stripped))
        cursor += len(part) + 1
    return tuple(cells)


def _profit_rate_table_hint(
    text: str,
    *,
    financing_document: bool,
) -> _RateContextHint | None:
    """Read a financing profit-rate hint out of a table header row, if safe.

    Issue #30: the hint fires only when exactly one header cell is a profit
    label, another cell carries the Vade/Ay term signature, and the table sits
    in a financing context.  A "Vade | Kar Payi Orani" table also exists on
    participation-account pages, where the percentage is a deposit distribution
    share — the financing requirement keeps that table from becoming a price.
    The period is asserted monthly: per-term installment tables quote the
    monthly kar orani, and every annual figure observed live carries its own
    label ("Yillik Maliyet Orani"), which excludes that column here.
    """

    if _NON_FINANCING_PROFIT_CONTEXT.search(text) is not None:
        return None
    cells = _table_cells(text)
    if len(cells) < 2:
        return None
    profit_columns = tuple(
        index
        for index, (_start, _end, cell) in enumerate(cells)
        if _PROFIT_RATE_COLUMN_LABEL.search(cell) is not None
        and _ANNUAL_OR_COST_COLUMN_LABEL.search(cell) is None
    )
    if len(profit_columns) != 1:
        return None
    profit_column = profit_columns[0]
    other_cells = tuple(
        cell for index, (_start, _end, cell) in enumerate(cells) if index != profit_column
    )
    if not any(_TERM_COLUMN_LABEL.search(cell) is not None for cell in other_cells):
        return None
    if not financing_document and _FINANCING_RATE_CONTEXT.search(text) is None:
        return None
    return _RateContextHint(
        kind=RateKind.FINANCING_PROFIT_RATE,
        period=RatePeriod.MONTHLY,
        profit_column=profit_column,
        column_count=len(cells),
        sole_rate_column=all(_RATE_LIKE_COLUMN_LABEL.search(cell) is None for cell in other_cells),
    )


def _profit_column_cell(sentence: TextSpan, hint: _RateContextHint) -> TextSpan | None:
    """Attribute the hinted profit column of a row to its exact cell span.

    A row that does not match the header's column count is attributed only when
    the header named a single rate-like column and the row carries a single
    percent-bearing cell; otherwise no attribution is made and the hint is
    withheld (issue #30: a missed rate is safe, a misattributed one is not).
    """

    cells = _table_cells(sentence.quote)
    selected: tuple[int, int, str] | None = None
    if hint.column_count is not None and len(cells) == hint.column_count:
        assert hint.profit_column is not None
        selected = cells[hint.profit_column]
    elif hint.sole_rate_column:
        percent_cells = tuple(cell for cell in cells if _RATE_MARKER.search(cell[2]) is not None)
        if len(percent_cells) == 1:
            selected = percent_cells[0]
    if selected is None:
        return None
    start, end, quote = selected
    if not quote or _RATE_MARKER.search(quote) is None:
        return None
    return TextSpan(
        sentence.block_id,
        quote,
        sentence.start_char + start,
        sentence.start_char + end,
    )


def _rate_context_hints(
    blocks: Iterable[tuple[str, str, str]],
    family_hits: Mapping[ProductFamily, TextSpan],
) -> dict[str, _RateContextHint]:
    hints: dict[str, _RateContextHint] = {}
    document_is_unambiguously_financing = set(family_hits) == {ProductFamily.FINANCING}
    financing_document = (
        ProductFamily.FINANCING in family_hits
        and ProductFamily.PARTICIPATION_ACCOUNT not in family_hits
    )
    current_table_hint: _RateContextHint | None = None

    for block_id, kind, text in blocks:
        if kind != "table":
            current_table_hint = None

        if kind == "table":
            table_hint = _profit_rate_table_hint(text, financing_document=financing_document)
            if table_hint is not None:
                current_table_hint = table_hint
                hints[block_id] = table_hint
                continue

        label = _GENERIC_PROFIT_RATE_LABEL.search(text)
        if label is not None:
            is_financing = (
                document_is_unambiguously_financing
                or _FINANCING_RATE_CONTEXT.search(text) is not None
            )
            has_conflict = _NON_FINANCING_PROFIT_CONTEXT.search(text) is not None
            hint = (
                _RateContextHint(
                    kind=RateKind.FINANCING_PROFIT_RATE,
                    period=(
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

        if (
            kind == "table"
            and _LTV_TABLE_LABEL.search(text) is not None
            and _AMOUNT_BRACKET_CONTEXT.search(text) is not None
        ):
            current_table_hint = _RateContextHint(kind=RateKind.LTV_RATIO, period=None)
            hints[block_id] = current_table_hint
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


def product_family_block_spans(
    document: CleanDocument,
) -> dict[ProductFamily, tuple[TextSpan, ...]]:
    """Collect each family's first-match span per safe block, for dominance checks.

    Issue #33: ``_pattern_hits`` records only one span per family, which cannot
    say whether a competing signal is a page-dominating theme or a single
    cross-product teaser.  The per-block hit count makes that measurable.
    """

    spans: dict[ProductFamily, list[TextSpan]] = {}
    for block in document.blocks:
        if is_obvious_prompt_injection(block.text):
            continue
        for family, patterns in _PRODUCT_PATTERNS.items():
            for pattern in patterns:
                match = pattern.search(block.text)
                if match is not None and 0 < len(match.group()) <= 500:
                    spans.setdefault(family, []).append(
                        TextSpan(block.id, match.group(), match.start(), match.end())
                    )
                    break
    return {family: tuple(items) for family, items in spans.items()}


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
    return read_fee(span.quote)


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
    if _VOUCHER_MARKER.search(lowered) is not None and _RAFFLE_MARKER.search(lowered) is None:
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
        if _RATE_MARKER.search(text) is not None and _FEE_PERCENT_CONTEXT.search(text) is None:
            context_hint = rate_context_hints.get(span.block_id)
            rate_span = span
            if context_hint is not None and context_hint.column_count is not None:
                # Issue #30: a column-shaped hint applies only to the header's
                # profit column; when the cell cannot be attributed the hint is
                # withheld and the row is read without it.
                cell_span = _profit_column_cell(span, context_hint)
                if cell_span is None:
                    context_hint = None
                else:
                    rate_span = cell_span
            normalized_rate = normalize_rate(rate_span.quote)
            if (
                context_hint is not None
                and normalized_rate.value is not None
                and normalized_rate.value.kind is RateKind.UNKNOWN
            ):
                normalized_rate = normalize_rate(
                    rate_span.quote,
                    kind_hint=context_hint.kind,
                    period_hint=context_hint.period,
                )
            # Issue #28: a percentage whose kind stays unknown is not recorded.
            # The field stays unresolved for the narrow model question instead;
            # a missed rate is safe, a mislabeled one inverts the comparison.
            if (
                normalized_rate.value is not None
                and normalized_rate.value.kind is not RateKind.UNKNOWN
            ):
                rates.append(
                    BoundFact(
                        normalized_rate.value,
                        rate_span,
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
    if new_customer_only is None:
        # Explicit universal wording ("tüm müşterilerimiz") grounds a False
        # only when no restriction phrase exists anywhere on the page; a
        # sentence that negates the phrase ("... yararlanamaz") is skipped.
        for sentence in all_sentences:
            if _NEGATIVE_SEGMENT_CONTEXT.search(sentence.quote) is not None:
                continue
            if _NEGATIVE_CHANNEL_CONTEXT.search(sentence.quote) is not None:
                continue
            universal = _new_customer_universal_match(sentence.quote)
            if universal is not None:
                start = sentence.start_char + universal.start("universal")
                end = sentence.start_char + universal.end("universal")
                new_customer_only = BoundFact(
                    False,
                    TextSpan(sentence.block_id, universal.group("universal"), start, end),
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
    if rate_semantics_unresolved(rate.value for rate in typed_rates):
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
    "infer_page_currency",
    "is_explicit_eligibility",
    "is_explicit_new_customer_restriction",
    "is_explicit_new_customer_universal",
    "is_single_currency_try_quote",
    "product_family_block_spans",
    "rate_semantics_unresolved",
    "segment_key_for_text",
    "supported_campaign_types",
    "supported_product_families",
    "supported_sales_channels",
    "with_issue",
]
