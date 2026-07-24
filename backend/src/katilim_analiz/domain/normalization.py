"""Deterministic, evidence-preserving normalization for Turkish campaign fields.

The functions in this module operate on one semantic field at a time.  They do
not inspect external state, use the process clock, or silently pick a value from
an ambiguous span.  A result envelope keeps missing, invalid and ambiguous
inputs distinct from an explicitly stated numeric zero.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from katilim_analiz.contracts import (
    EvidenceStatus,
    GrossNetBasis,
    MoneyValue,
    RateKind,
    RatePeriod,
    RateValue,
    TermRange,
    ValidityWindow,
)


class NormalizationStatus(StrEnum):
    """Stable outcomes for deterministic normalization attempts."""

    NORMALIZED = "normalized"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class NormalizationResult[T]:
    """A normalized value or an explicit explanation for abstaining."""

    raw: str | None
    value: T | None
    status: NormalizationStatus
    reason_code: str
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        has_value = self.value is not None
        if has_value != (self.status is NormalizationStatus.NORMALIZED):
            raise ValueError("only normalized results may contain a value")

    @property
    def is_normalized(self) -> bool:
        return self.status is NormalizationStatus.NORMALIZED


_TURKISH_ASCII_TRANSLATION = str.maketrans(
    {
        "Ç": "C",
        "Ğ": "G",
        "İ": "I",
        "I": "I",
        "Ö": "O",
        "Ş": "S",
        "Ü": "U",
        "Â": "A",
        "Î": "I",
        "Û": "U",
        "ç": "c",
        "ğ": "g",
        "ı": "i",
        "ö": "o",
        "ş": "s",
        "ü": "u",
        "â": "a",
        "î": "i",
        "û": "u",
        "−": "-",
        "–": "-",
        "—": "-",
        "’": "'",
        "`": "'",
    }
)


def canonicalize_turkish_text(value: str) -> str:
    """Return an accent-insensitive canonical key without changing the source."""

    normalized = unicodedata.normalize("NFKC", value).translate(_TURKISH_ASCII_TRANSLATION)
    lowered = normalized.casefold()
    words_only = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(words_only.split())


def _search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).translate(_TURKISH_ASCII_TRANSLATION)
    return normalized.casefold().replace("\u00a0", " ").replace("\u202f", " ")


_MISSING_VALUES = {
    "",
    "-",
    "n a",
    "na",
    "none",
    "null",
    "unknown",
    "bilinmiyor",
    "bilinmeyen",
    "belirtilmemis",
    "mevcut degil",
    "bulunamadi",
    "not found",
}
_MISSING_FIELD_RE = re.compile(
    r"\b(?:bilgi(?:si)?|oran(?:i)?|tutar(?:i)?|vade(?:si)?|tarih(?:i)?|ucret(?:i)?)\s+"
    r"(?:belirtilmemis|bulunmuyor|bulunamadi|bilinmiyor|yok)\b"
)


def _is_missing(raw: str | None) -> bool:
    if raw is None:
        return True
    canonical = canonicalize_turkish_text(raw)
    return canonical in _MISSING_VALUES or _MISSING_FIELD_RE.search(canonical) is not None


def _failure[T](
    raw: str | None,
    status: NormalizationStatus,
    reason_code: str,
    *warnings: str,
) -> NormalizationResult[T]:
    return NormalizationResult(
        raw=raw,
        value=None,
        status=status,
        reason_code=reason_code,
        warnings=tuple(warnings),
    )


_NUMBER_TOKEN_RE = re.compile(
    r"(?<![\w])"
    r"[+-]?(?:\d{1,3}(?:[.,\s\u00a0\u202f]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"(?![\w])"
)
_SCALE_FACTORS = {
    "bin": Decimal("1000"),
    "milyon": Decimal("1000000"),
    "milyar": Decimal("1000000000"),
}
_CURRENCY_WORD_RE = re.compile(
    r"(?:(?<![a-z0-9])(?:turk\s+lirasi|abd\s+dolari|amerikan\s+dolari|"
    r"tl|try|usd|eur|euro|avro|gbp|sterlin)(?![a-z0-9])|[₺€£$])"
)
_CURRENCY_CODES = {
    "turk lirasi": "TRY",
    "tl": "TRY",
    "try": "TRY",
    "₺": "TRY",
    "abd dolari": "USD",
    "amerikan dolari": "USD",
    "usd": "USD",
    "eur": "EUR",
    "euro": "EUR",
    "avro": "EUR",
    "€": "EUR",
    "gbp": "GBP",
    "sterlin": "GBP",
    "£": "GBP",
}
_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_ZERO_FEE_PATTERNS = (
    re.compile(r"\bucretsiz\b"),
    re.compile(r"\bmasrafsiz\b"),
    re.compile(r"\bucret\s+alinma(?:z|yacaktir)\b"),
    re.compile(r"\bucret\s+tahsil\s+edilme(?:z|yecektir)\b"),
    re.compile(r"\b(?:tahsis\s+)?ucret(?:i)?\s+yoktur\b"),
    re.compile(r"\bherhangi\s+bir\s+ucret\s+(?:yoktur|bulunmamaktadir)\b"),
)
_NEGATED_ZERO_FEE_RE = re.compile(r"\b(?:ucretsiz|masrafsiz)\s+degil\b|\bucret\s+alinmaz\s+degil\b")


def _currency_code(currency_marker: str, currency_hint: str | None) -> str | None:
    if currency_marker == "$":
        return "USD" if currency_hint == "USD" else None
    return _CURRENCY_CODES.get(" ".join(currency_marker.split()))


def _validated_currency_hint(currency_hint: str | None) -> str | None:
    if currency_hint is None:
        return None
    normalized = currency_hint.strip().upper()
    if _ISO_CURRENCY_RE.fullmatch(normalized) is None:
        raise ValueError("currency_hint must be an ISO 4217-style three-letter code")
    return normalized


def _parse_decimal_token(token: str, *, currency: str | None, scale: str | None) -> Decimal:
    compact = re.sub(r"[\s\u00a0\u202f]", "", token)
    if compact.startswith("-"):
        raise ValueError("negative values are not allowed")
    compact = compact.removeprefix("+")
    if not compact or not re.fullmatch(r"\d[\d.,]*", compact):
        raise ValueError("invalid numeric token")

    comma_count = compact.count(",")
    dot_count = compact.count(".")
    decimal_separator: str | None = None

    if comma_count and dot_count:
        decimal_separator = "," if compact.rfind(",") > compact.rfind(".") else "."
    elif comma_count > 1:
        groups = compact.split(",")
        if not all(len(group) == 3 for group in groups[1:]):
            raise ValueError("invalid comma grouping")
    elif dot_count > 1:
        groups = compact.split(".")
        if not all(len(group) == 3 for group in groups[1:]):
            raise ValueError("invalid dot grouping")
    elif comma_count == 1:
        decimal_separator = ","
    elif dot_count == 1:
        fractional_length = len(compact.rsplit(".", 1)[1])
        if fractional_length == 3:
            # Turkish sources group thousands with a dot regardless of the
            # currency token ("10.000 USD" is ten thousand dollars).  Reading
            # the dot as a decimal point for foreign currencies collapsed the
            # magnitude by a thousand, which is a wrong value, not a miss.
            decimal_separator = None
        elif currency == "TRY":
            raise ValueError("TRY decimals must use a comma")
        else:
            decimal_separator = "."

    if decimal_separator is None:
        canonical_number = compact.replace(",", "").replace(".", "")
    else:
        grouping_separator = "." if decimal_separator == "," else ","
        integer_part, fractional_part = compact.rsplit(decimal_separator, 1)
        integer_groups = integer_part.split(grouping_separator)
        if len(integer_groups) > 1 and not all(len(group) == 3 for group in integer_groups[1:]):
            raise ValueError("invalid thousands grouping")
        canonical_number = "".join(integer_groups) + "." + fractional_part

    try:
        value = Decimal(canonical_number)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    return value * _SCALE_FACTORS.get(scale or "", Decimal("1"))


@dataclass(frozen=True, slots=True)
class _MoneyCandidate:
    token: str
    scale: str | None
    currency: str
    span: tuple[int, int]


def _money_candidates(text: str, currency_hint: str | None) -> tuple[_MoneyCandidate, ...]:
    numbers = tuple(_NUMBER_TOKEN_RE.finditer(text))
    currencies = tuple(_CURRENCY_WORD_RE.finditer(text))
    candidates: list[_MoneyCandidate] = []

    for number in numbers:
        matches: list[tuple[re.Match[str], str | None]] = []
        for currency in currencies:
            if currency.end() <= number.start():
                gap = text[currency.end() : number.start()]
                if re.fullmatch(r"\s*", gap):
                    suffix = text[number.end() :]
                    scale_match = re.match(r"\s*(bin|milyon|milyar)\b", suffix)
                    matches.append(
                        (currency, None if scale_match is None else scale_match.group(1))
                    )
            elif number.end() <= currency.start():
                gap = text[number.end() : currency.start()]
                scale_match = re.fullmatch(r"\s*(bin|milyon|milyar)?\s*", gap)
                if scale_match is not None:
                    matches.append((currency, scale_match.group(1)))

        if not matches and not currencies and currency_hint is not None:
            suffix = text[number.end() :]
            scale_match = re.match(r"\s*(bin|milyon|milyar)\b", suffix)
            candidates.append(
                _MoneyCandidate(
                    token=number.group(),
                    scale=None if scale_match is None else scale_match.group(1),
                    currency=currency_hint,
                    span=number.span(),
                )
            )
            continue

        for currency_match, scale in matches:
            code = _currency_code(currency_match.group(), currency_hint)
            if code is None:
                continue
            if currency_hint is not None and code != currency_hint:
                continue
            candidates.append(
                _MoneyCandidate(
                    token=number.group(),
                    scale=scale,
                    currency=code,
                    span=(
                        min(number.start(), currency_match.start()),
                        max(number.end(), currency_match.end()),
                    ),
                )
            )

    unique = {(item.token, item.scale, item.currency, item.span): item for item in candidates}
    return tuple(sorted(unique.values(), key=lambda item: item.span))


def normalize_money(
    raw: str | None,
    *,
    currency_hint: str | None = None,
    allow_fee_waiver: bool = False,
) -> NormalizationResult[MoneyValue]:
    """Normalize one monetary field without assuming a hidden currency.

    ``currency_hint`` is an evidence-backed context supplied by the caller.  A
    generic phrase such as ``ücretsiz`` becomes zero only when the caller marks
    the field as fee-like and provides that currency context.
    """

    hint = _validated_currency_hint(currency_hint)
    if _is_missing(raw):
        return _failure(raw, NormalizationStatus.MISSING, "money_missing")
    assert raw is not None
    text = _search_text(raw.strip())

    if _NEGATED_ZERO_FEE_RE.search(text) is not None:
        return _failure(raw, NormalizationStatus.INVALID, "fee_waiver_negated")

    explicit_waiver = any(pattern.search(text) is not None for pattern in _ZERO_FEE_PATTERNS)
    if explicit_waiver:
        if not allow_fee_waiver:
            return _failure(raw, NormalizationStatus.UNSUPPORTED, "fee_waiver_requires_fee_context")
        if hint is None:
            return _failure(raw, NormalizationStatus.AMBIGUOUS, "money_currency_unknown")
        return NormalizationResult(
            raw=raw,
            value=MoneyValue(
                raw=raw,
                amount=Decimal("0"),
                currency=hint,
                status=EvidenceStatus.INFERRED,
            ),
            status=NormalizationStatus.NORMALIZED,
            reason_code="explicit_fee_waiver_normalized",
        )

    mentioned_codes = {
        _currency_code(match.group(), hint) for match in _CURRENCY_WORD_RE.finditer(text)
    }
    if None in mentioned_codes:
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "currency_symbol_ambiguous")
    explicit_codes = {code for code in mentioned_codes if code is not None}
    if hint is not None and explicit_codes and explicit_codes != {hint}:
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "currency_hint_conflict")
    if not explicit_codes and hint is None:
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "money_currency_unknown")
    if len(tuple(_NUMBER_TOKEN_RE.finditer(text))) != 1:
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "multiple_money_values")

    candidates = _money_candidates(text, hint)
    if not candidates:
        return _failure(raw, NormalizationStatus.INVALID, "money_value_not_found")
    if len(candidates) != 1:
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "multiple_money_values")

    candidate = candidates[0]
    try:
        amount = _parse_decimal_token(
            candidate.token,
            currency=candidate.currency,
            scale=candidate.scale,
        )
    except ValueError:
        return _failure(raw, NormalizationStatus.INVALID, "invalid_money_number")

    evidence_status = EvidenceStatus.STATED if explicit_codes else EvidenceStatus.INFERRED
    warnings: tuple[str, ...] = ()
    if (
        re.search(r"\b(?:bsmv|kkdf|vergi)\s*(?:haric|dahil)", text) is not None
        or re.search(r"\+\s*(?:bsmv|kkdf|vergi)\b", text) is not None
    ):
        warnings = ("money_tax_basis_requires_structured_context",)
    return NormalizationResult(
        raw=raw,
        value=MoneyValue(
            raw=raw,
            amount=amount,
            currency=candidate.currency,
            status=evidence_status,
        ),
        status=NormalizationStatus.NORMALIZED,
        reason_code="money_normalized",
        warnings=warnings,
    )


_RATE_NUMBER = r"[+-]?\d+(?:[.,]\d+)?"
_RATE_PATTERNS = (
    re.compile(rf"%\s*(?P<number>{_RATE_NUMBER})"),
    re.compile(rf"(?P<number>{_RATE_NUMBER})\s*%"),
    re.compile(rf"\byuzde\s+(?P<number>{_RATE_NUMBER})\b"),
)
_PER_MILLE_PATTERNS = (
    re.compile(rf"‰\s*(?P<number>{_RATE_NUMBER})"),
    re.compile(rf"\bbinde\s+(?P<number>{_RATE_NUMBER})\b"),
)
_BASIS_POINT_RE = re.compile(rf"\b(?P<number>{_RATE_NUMBER})\s+baz\s+puan\b")
_RATE_BOUND_RE = re.compile(
    r"\b(?:baslayan|itibaren|azami|asgari|en\s+fazla|en\s+az|arasi|arasinda)\b"
)
#: "1 Temmuz 2026 tarihinden itibaren gecerli oran" dates the rate's validity;
#: the "itibaren" there is not a from-bound on the rate value itself.  The
#: phrase is removed before the bound check so a dated in-force rate can still
#: normalize; a bare "%3'ten itibaren" keeps refusing as a bound.
_DATE_FROM_VALIDITY_RE = re.compile(r"\btarih(?:in)?[dt]en\s+itibaren\b")


def _parse_rate_number(token: str) -> Decimal:
    compact = token.replace(",", ".")
    try:
        value = Decimal(compact)
    except InvalidOperation as exc:
        raise ValueError("invalid rate") from exc
    if value < 0 or value > Decimal("1000"):
        raise ValueError("rate outside contract bounds")
    return value


#: Issue #28: a percentage of the asset's value is a financing share (LTV),
#: not a price.  "Azami/maksimum finansman orani" and "degerinin %X'i" state
#: that share explicitly; both phrasings came from live financing pages.
_LTV_CONTEXT_RE = re.compile(
    r"\b(?:azami|maksimum|maks)\s+finansman\s+oran"
    r"|\bdeger(?:i|si)?nin\s+(?:en\s+fazla\s+|azami\s+)?(?:%|yuzde\b)"
)
#: A stated money amount next to "finansman orani" is the signature of a
#: value-bracket (tutar araligi | % | vade) row, where the percentage is the
#: financing share, not a profit rate.  The pairing is ambiguous, so no kind
#: is asserted (issue #28: a missed rate is safe, an inverted label is not).
_AMOUNT_NEAR_RATE_RE = re.compile(r"(?:\d[\d.,]*\s*(?:tl|try)\b|₺\s*\d)")


def _infer_rate_kind(text: str) -> RateKind:
    if re.search(r"\byillik\s+(?:toplam\s+)?maliyet\s+oran", text):
        return RateKind.ANNUAL_COST_RATE
    if _LTV_CONTEXT_RE.search(text):
        return RateKind.LTV_RATIO
    if re.search(r"\b(?:kar\s+(?:payi\s+)?dagitim|kar\s+paylasim)\s+oran", text):
        return RateKind.PROFIT_SHARE_DISTRIBUTION_RATE
    if re.search(r"\b(?:gecmis|tarihsel|gerceklesen)\s+(?:yillik\s+)?getiri", text):
        return RateKind.HISTORICAL_RETURN_RATE
    if re.search(r"\bindirim(?:\s+orani)?\b", text):
        return RateKind.DISCOUNT_RATE
    if re.search(r"\b(?:nakit\s+)?iade\s+orani\b|\bodul\s+orani\b", text):
        return RateKind.REWARD_RATE
    if re.search(r"\b(?:finansman(?:\s+kar\s+payi)?|kullandirim\s+kar\s+payi)\s+orani\b", text):
        if re.search(r"\bkar\b", text) is None and _AMOUNT_NEAR_RATE_RE.search(text) is not None:
            return RateKind.UNKNOWN
        return RateKind.FINANCING_PROFIT_RATE
    return RateKind.UNKNOWN


def _infer_rate_period(text: str) -> RatePeriod:
    if re.search(r"\b(?:vade\s+sonu(?:nda)?|vade\s+toplami|toplam\s+oran)\b", text):
        return RatePeriod.TERM_TOTAL
    if re.search(r"\byillik\b|\bsenelik\b", text):
        return RatePeriod.ANNUAL
    if re.search(r"\baylik\b", text):
        return RatePeriod.MONTHLY
    return RatePeriod.UNSPECIFIED


def _infer_gross_net(text: str) -> GrossNetBasis:
    has_gross = re.search(r"\bbrut\b", text) is not None
    has_net = re.search(r"\bnet\b", text) is not None
    if has_gross == has_net:
        return GrossNetBasis.UNSPECIFIED
    return GrossNetBasis.GROSS if has_gross else GrossNetBasis.NET


def _infer_rate_basis_label(text: str) -> str | None:
    labels = (
        (r"\befektif\b", "effective"),
        (r"\bnominal\b", "nominal"),
        (r"\bbilesik\b", "compound"),
        (r"\bbasit\b", "simple"),
    )
    found = [label for pattern, label in labels if re.search(pattern, text)]
    return found[0] if len(found) == 1 else None


def normalize_rate(
    raw: str | None,
    *,
    kind_hint: RateKind | None = None,
    period_hint: RatePeriod | None = None,
    gross_net_hint: GrossNetBasis | None = None,
    term_months_hint: int | None = None,
    basis_label_hint: str | None = None,
) -> NormalizationResult[RateValue]:
    """Normalize one exact percentage and its stated or caller-bound semantics."""

    if _is_missing(raw):
        return _failure(raw, NormalizationStatus.MISSING, "rate_missing")
    assert raw is not None
    text = _search_text(raw.strip())

    matches: list[tuple[tuple[int, int], Decimal]] = []
    for pattern in _RATE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                value = _parse_rate_number(match.group("number"))
            except ValueError:
                return _failure(raw, NormalizationStatus.INVALID, "invalid_rate_number")
            matches.append((match.span(), value))
    for pattern in _PER_MILLE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                value = _parse_rate_number(match.group("number")) / Decimal("10")
            except ValueError:
                return _failure(raw, NormalizationStatus.INVALID, "invalid_rate_number")
            matches.append((match.span(), value))
    for match in _BASIS_POINT_RE.finditer(text):
        try:
            value = _parse_rate_number(match.group("number")) / Decimal("100")
        except ValueError:
            return _failure(raw, NormalizationStatus.INVALID, "invalid_rate_number")
        matches.append((match.span(), value))

    unique_matches = {(span, value) for span, value in matches}
    if not unique_matches:
        return _failure(raw, NormalizationStatus.INVALID, "rate_value_not_found")
    bound_text = _DATE_FROM_VALIDITY_RE.sub(" ", text)
    if len(unique_matches) != 1 or _RATE_BOUND_RE.search(bound_text) is not None:
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "non_exact_or_multiple_rates")
    value_percent = next(iter(unique_matches))[1]

    inferred_kind = _infer_rate_kind(text)
    inferred_period = _infer_rate_period(text)
    inferred_gross_net = _infer_gross_net(text)
    if (
        kind_hint is not None
        and inferred_kind is not RateKind.UNKNOWN
        and kind_hint is not inferred_kind
    ):
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "rate_kind_hint_conflict")
    if (
        period_hint is not None
        and inferred_period is not RatePeriod.UNSPECIFIED
        and period_hint is not inferred_period
    ):
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "rate_period_hint_conflict")
    if (
        gross_net_hint is not None
        and inferred_gross_net is not GrossNetBasis.UNSPECIFIED
        and gross_net_hint is not inferred_gross_net
    ):
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "gross_net_hint_conflict")

    kind = kind_hint or inferred_kind
    period = period_hint or inferred_period
    gross_net = gross_net_hint or inferred_gross_net
    used_hint = any(
        hint is not None
        for hint in (kind_hint, period_hint, gross_net_hint, term_months_hint, basis_label_hint)
    )
    status = EvidenceStatus.INFERRED if used_hint else EvidenceStatus.STATED
    basis_label = basis_label_hint or _infer_rate_basis_label(text)
    return NormalizationResult(
        raw=raw,
        value=RateValue(
            raw=raw,
            value_percent=value_percent,
            kind=kind,
            period=period,
            gross_net_basis=gross_net,
            term_months=term_months_hint,
            basis_label=basis_label,
            status=status,
        ),
        status=NormalizationStatus.NORMALIZED,
        reason_code="rate_normalized",
    )


_TERM_NUMBER = r"\d+(?:[.,]\d+)?"
_TERM_UNIT = r"(?:aylik|ay|yillik|yil|senelik|sene)"


def _months(number: str, unit: str) -> int:
    try:
        value = Decimal(number.replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("invalid term number") from exc
    months = value if unit.startswith("ay") else value * Decimal("12")
    if months != months.to_integral_value() or months <= 0 or months > 1200:
        raise ValueError("term is not a supported whole-month duration")
    return int(months)


def _term(raw: str, minimum: int | None, maximum: int) -> TermRange:
    return TermRange(raw=raw, minimum_months=minimum, maximum_months=maximum)


def normalize_terms(raw: str | None) -> NormalizationResult[tuple[TermRange, ...]]:
    """Normalize an exact/ranged term or preserve discrete options separately."""

    if _is_missing(raw):
        return _failure(raw, NormalizationStatus.MISSING, "term_missing")
    assert raw is not None
    text = _search_text(raw.strip())
    if re.search(r"\b(?:gun|hafta)\b", text):
        return _failure(raw, NormalizationStatus.UNSUPPORTED, "non_month_term_unit")
    if re.search(r"\b(?:ve\s+uzeri|den\s+fazla|asgariden\s+itibaren)\b", text):
        return _failure(raw, NormalizationStatus.UNSUPPORTED, "open_ended_term_not_representable")
    if (
        re.search(r"\btaksit\b", text)
        and re.search(rf"{_TERM_NUMBER}\s*taksit", text)
        and re.search(rf"{_TERM_NUMBER}\s*ay\s+taksit", text) is None
    ):
        return _failure(raw, NormalizationStatus.UNSUPPORTED, "installment_count_is_not_term")
    if "erteleme" in text and ("taksit" in text or "vade" in text):
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "multiple_term_roles")

    if re.search(r"\b(?:secenek|taksit)\w*\b", text) and re.search(r"[,/]\s*\d|\d\s+ve\s+\d", text):
        options = [int(token) for token in re.findall(r"\b\d+\b", text)]
        if len(options) >= 2 and all(0 < option <= 1200 for option in options):
            values = tuple(_term(raw, option, option) for option in dict.fromkeys(options))
            return NormalizationResult(
                raw=raw,
                value=values,
                status=NormalizationStatus.NORMALIZED,
                reason_code="discrete_terms_normalized",
            )

    range_pattern = re.compile(
        rf"(?P<first>{_TERM_NUMBER})\s*(?P<first_unit>{_TERM_UNIT})?\s*"
        rf"(?:-|ile|ila)\s*(?P<second>{_TERM_NUMBER})\s*(?P<second_unit>{_TERM_UNIT})\b"
    )
    range_match = range_pattern.search(text)
    if range_match is not None:
        first_unit = range_match.group("first_unit") or range_match.group("second_unit")
        try:
            minimum = _months(range_match.group("first"), first_unit)
            maximum = _months(range_match.group("second"), range_match.group("second_unit"))
        except ValueError:
            return _failure(raw, NormalizationStatus.INVALID, "invalid_term_range")
        if minimum > maximum:
            return _failure(raw, NormalizationStatus.INVALID, "reversed_term_range")
        return NormalizationResult(
            raw=raw,
            value=(_term(raw, minimum, maximum),),
            status=NormalizationStatus.NORMALIZED,
            reason_code="term_range_normalized",
        )

    suffix_range = re.search(
        rf"(?P<first>{_TERM_NUMBER})\s*(?P<first_unit>ay|yil|sene)dan\s+"
        rf"(?P<second>{_TERM_NUMBER})\s*(?P<second_unit>ay|yil|sene)a\s+kadar",
        text,
    )
    if suffix_range is not None:
        try:
            minimum = _months(suffix_range.group("first"), suffix_range.group("first_unit"))
            maximum = _months(suffix_range.group("second"), suffix_range.group("second_unit"))
        except ValueError:
            return _failure(raw, NormalizationStatus.INVALID, "invalid_term_range")
        if minimum > maximum:
            return _failure(raw, NormalizationStatus.INVALID, "reversed_term_range")
        return NormalizationResult(
            raw=raw,
            value=(_term(raw, minimum, maximum),),
            status=NormalizationStatus.NORMALIZED,
            reason_code="term_range_normalized",
        )

    maximum_match = re.search(
        rf"(?:en\s+fazla|azami)\s+(?P<number>{_TERM_NUMBER})\s*(?P<unit>{_TERM_UNIT})\b",
        text,
    ) or re.search(
        rf"(?P<number>{_TERM_NUMBER})\s*(?P<unit>ay|yil|sene)a\s+"
        rf"(?:kadar|varan)\b",
        text,
    )
    if maximum_match is not None:
        try:
            maximum = _months(maximum_match.group("number"), maximum_match.group("unit"))
        except ValueError:
            return _failure(raw, NormalizationStatus.INVALID, "invalid_term_maximum")
        return NormalizationResult(
            raw=raw,
            value=(_term(raw, None, maximum),),
            status=NormalizationStatus.NORMALIZED,
            reason_code="maximum_term_normalized",
        )

    mixed_duration = re.search(
        rf"(?P<years>{_TERM_NUMBER})\s*(?:yil|sene)\s+"
        rf"(?P<months>{_TERM_NUMBER})\s*ay\b",
        text,
    )
    if mixed_duration is not None:
        try:
            total = _months(mixed_duration.group("years"), "yil") + _months(
                mixed_duration.group("months"), "ay"
            )
        except ValueError:
            return _failure(raw, NormalizationStatus.INVALID, "invalid_mixed_term")
        if total > 1200:
            return _failure(raw, NormalizationStatus.INVALID, "term_out_of_range")
        return NormalizationResult(
            raw=raw,
            value=(_term(raw, total, total),),
            status=NormalizationStatus.NORMALIZED,
            reason_code="mixed_term_normalized",
        )

    exact_matches = tuple(
        re.finditer(rf"(?P<number>{_TERM_NUMBER})\s*(?P<unit>{_TERM_UNIT})\b", text)
    )
    if not exact_matches:
        return _failure(raw, NormalizationStatus.INVALID, "term_value_not_found")
    if len(exact_matches) != 1:
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "multiple_term_values")
    match = exact_matches[0]
    try:
        months = _months(match.group("number"), match.group("unit"))
    except ValueError:
        return _failure(raw, NormalizationStatus.INVALID, "invalid_term_number")
    return NormalizationResult(
        raw=raw,
        value=(_term(raw, months, months),),
        status=NormalizationStatus.NORMALIZED,
        reason_code="exact_term_normalized",
    )


def normalize_term(raw: str | None) -> NormalizationResult[TermRange]:
    """Normalize a single term; abstain when the source states discrete options."""

    result = normalize_terms(raw)
    if result.value is None:
        return NormalizationResult(
            raw=result.raw,
            value=None,
            status=result.status,
            reason_code=result.reason_code,
            warnings=result.warnings,
        )
    if len(result.value) != 1:
        return _failure(
            raw,
            NormalizationStatus.AMBIGUOUS,
            "multiple_discrete_terms",
            "use_normalize_terms_for_discrete_options",
        )
    return NormalizationResult(
        raw=result.raw,
        value=result.value[0],
        status=NormalizationStatus.NORMALIZED,
        reason_code=result.reason_code,
        warnings=result.warnings,
    )


_MONTHS = {
    "ocak": 1,
    "subat": 2,
    "mart": 3,
    "nisan": 4,
    "mayis": 5,
    "haziran": 6,
    "temmuz": 7,
    "agustos": 8,
    "eylul": 9,
    "ekim": 10,
    "kasim": 11,
    "aralik": 12,
}
_MONTH_PATTERN = "|".join(_MONTHS)


@dataclass(frozen=True, slots=True)
class _DateParts:
    day: int
    month: int
    year: int | None
    span: tuple[int, int]


def _extract_date_parts(text: str) -> tuple[tuple[_DateParts, ...], bool]:
    candidates: list[_DateParts] = []
    invalid = False
    patterns = (
        re.compile(r"(?<!\d)(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?!\d)"),
        re.compile(r"(?<!\d)(?P<day>\d{1,2})[./](?P<month>\d{1,2})[./](?P<year>\d{4})(?!\d)"),
        re.compile(
            rf"(?<!\d)(?P<day>\d{{1,2}})\s+(?P<month>{_MONTH_PATTERN})"
            r"(?:\s+(?P<year>\d{4}))?(?!\d)"
        ),
        re.compile(
            r"(?<![\d.])(?P<day>\d{1,2})[./](?P<month>\d{1,2})"
            r"(?!\d|[./]\d)"
        ),
    )
    occupied: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in occupied):
                continue
            month_token = match.group("month")
            month = _MONTHS[month_token] if not month_token.isdigit() else int(month_token)
            year_token = match.groupdict().get("year")
            year = None if year_token is None else int(year_token)
            part = _DateParts(int(match.group("day")), month, year, match.span())
            if year is not None:
                try:
                    date(year, month, part.day)
                except ValueError:
                    invalid = True
            candidates.append(part)
            occupied.append(match.span())
    return tuple(sorted(candidates, key=lambda item: item.span)), invalid


def _date_from_parts(parts: _DateParts, year: int) -> date:
    return date(year, parts.month, parts.day)


def normalize_date(
    raw: str | None,
    *,
    reference_date: date | None = None,
) -> NormalizationResult[date]:
    """Normalize one calendar date; relative dates require an injected reference."""

    if _is_missing(raw):
        return _failure(raw, NormalizationStatus.MISSING, "date_missing")
    assert raw is not None
    text = _search_text(raw.strip())
    canonical = canonicalize_turkish_text(raw)

    if canonical in {"bugun", "yarin"}:
        if reference_date is None:
            return _failure(raw, NormalizationStatus.AMBIGUOUS, "relative_date_needs_reference")
        value = reference_date if canonical == "bugun" else reference_date + timedelta(days=1)
        return NormalizationResult(
            raw=raw,
            value=value,
            status=NormalizationStatus.NORMALIZED,
            reason_code="relative_date_normalized",
            warnings=("date_inferred_from_reference_date",),
        )

    parts, invalid = _extract_date_parts(text)
    if invalid:
        return _failure(raw, NormalizationStatus.INVALID, "invalid_calendar_date")
    if len(parts) > 1:
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "multiple_dates")
    if not parts:
        if re.search(rf"\b(?:{_MONTH_PATTERN})\s+\d{{4}}\b", text):
            return _failure(raw, NormalizationStatus.AMBIGUOUS, "date_precision_ambiguous")
        return _failure(raw, NormalizationStatus.INVALID, "date_value_not_found")
    part = parts[0]
    warnings: tuple[str, ...] = ()
    if part.year is None:
        if reference_date is None:
            return _failure(raw, NormalizationStatus.AMBIGUOUS, "date_year_missing")
        year = reference_date.year
        warnings = ("year_inferred_from_reference_date",)
    else:
        year = part.year
    try:
        value = _date_from_parts(part, year)
    except ValueError:
        return _failure(raw, NormalizationStatus.INVALID, "invalid_calendar_date")
    return NormalizationResult(
        raw=raw,
        value=value,
        status=NormalizationStatus.NORMALIZED,
        reason_code="date_normalized",
        warnings=warnings,
    )


def _resolve_date_pair(
    first: _DateParts,
    second: _DateParts,
    reference_date: date | None,
) -> tuple[date, date] | None:
    first_year = first.year
    second_year = second.year
    if first_year is None and second_year is None:
        if reference_date is None:
            return None
        first_year = second_year = reference_date.year
    elif first_year is None:
        assert second_year is not None
        first_year = (
            second_year
            if (first.month, first.day) <= (second.month, second.day)
            else second_year - 1
        )
    elif second_year is None:
        second_year = (
            first_year if (second.month, second.day) >= (first.month, first.day) else first_year + 1
        )
    try:
        return _date_from_parts(first, first_year), _date_from_parts(second, second_year)
    except ValueError:
        return None


def normalize_validity(
    raw: str | None,
    *,
    reference_date: date | None = None,
) -> NormalizationResult[ValidityWindow]:
    """Normalize a closed or explicitly open Turkish validity window."""

    if _is_missing(raw):
        return _failure(raw, NormalizationStatus.MISSING, "validity_missing")
    assert raw is not None
    text = _search_text(raw.strip())
    if re.search(r"\b\d{1,2}:\d{2}\b", text):
        return _failure(raw, NormalizationStatus.UNSUPPORTED, "time_of_day_not_representable")

    if "bu ay sonuna kadar" in text:
        if reference_date is None:
            return _failure(raw, NormalizationStatus.AMBIGUOUS, "relative_date_needs_reference")
        last_day = calendar.monthrange(reference_date.year, reference_date.month)[1]
        value = ValidityWindow(
            raw=raw,
            ends_on=date(reference_date.year, reference_date.month, last_day),
            status=EvidenceStatus.INFERRED,
        )
        return NormalizationResult(
            raw=raw,
            value=value,
            status=NormalizationStatus.NORMALIZED,
            reason_code="relative_validity_normalized",
            warnings=("date_inferred_from_reference_date",),
        )

    same_month = re.search(
        rf"(?<!\d)(?P<first>\d{{1,2}})\s*-\s*(?P<second>\d{{1,2}})\s+"
        rf"(?P<month>{_MONTH_PATTERN})\s+(?P<year>\d{{4}})(?!\d)",
        text,
    )
    if same_month is not None:
        try:
            starts_on = date(
                int(same_month.group("year")),
                _MONTHS[same_month.group("month")],
                int(same_month.group("first")),
            )
            ends_on = date(
                int(same_month.group("year")),
                _MONTHS[same_month.group("month")],
                int(same_month.group("second")),
            )
        except ValueError:
            return _failure(raw, NormalizationStatus.INVALID, "invalid_validity_window")
        if starts_on > ends_on:
            return _failure(raw, NormalizationStatus.INVALID, "reversed_validity_window")
        return NormalizationResult(
            raw=raw,
            value=ValidityWindow(raw=raw, starts_on=starts_on, ends_on=ends_on),
            status=NormalizationStatus.NORMALIZED,
            reason_code="validity_range_normalized",
        )

    full_month = re.search(
        rf"\b(?P<month>{_MONTH_PATTERN})\s+(?P<year>\d{{4}})\s+(?:ayi\s+)?boyunca\b",
        text,
    )
    if full_month is not None:
        year = int(full_month.group("year"))
        month = _MONTHS[full_month.group("month")]
        last_day = calendar.monthrange(year, month)[1]
        return NormalizationResult(
            raw=raw,
            value=ValidityWindow(
                raw=raw,
                starts_on=date(year, month, 1),
                ends_on=date(year, month, last_day),
            ),
            status=NormalizationStatus.NORMALIZED,
            reason_code="full_month_validity_normalized",
        )

    parts, invalid = _extract_date_parts(text)
    if invalid:
        return _failure(raw, NormalizationStatus.INVALID, "invalid_validity_window")
    if len(parts) == 2:
        if "son basvuru" in text and "kullanim" in text:
            return _failure(raw, NormalizationStatus.AMBIGUOUS, "multiple_validity_roles")
        between = text[parts[0].span[1] : parts[1].span[0]]
        if re.search(r"-|\b(?:ile|ila|dan|den)\b", between) is None:
            return _failure(raw, NormalizationStatus.AMBIGUOUS, "multiple_validity_dates")
        resolved = _resolve_date_pair(parts[0], parts[1], reference_date)
        if resolved is None:
            return _failure(raw, NormalizationStatus.AMBIGUOUS, "validity_year_missing")
        starts_on, ends_on = resolved
        if starts_on > ends_on:
            return _failure(raw, NormalizationStatus.INVALID, "reversed_validity_window")
        warnings = (
            ("year_inferred_from_reference_date",)
            if parts[0].year is None and parts[1].year is None
            else ()
        )
        return NormalizationResult(
            raw=raw,
            value=ValidityWindow(raw=raw, starts_on=starts_on, ends_on=ends_on),
            status=NormalizationStatus.NORMALIZED,
            reason_code="validity_range_normalized",
            warnings=warnings,
        )
    if not parts and re.search(rf"\b(?:{_MONTH_PATTERN})\s+\d{{4}}\b", text):
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "validity_precision_ambiguous")
    if len(parts) != 1:
        return _failure(raw, NormalizationStatus.INVALID, "validity_date_not_found")

    part = parts[0]
    if part.year is None and reference_date is None:
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "validity_year_missing")
    if part.year is not None:
        year = part.year
    else:
        assert reference_date is not None
        year = reference_date.year
    try:
        boundary = _date_from_parts(part, year)
    except ValueError:
        return _failure(raw, NormalizationStatus.INVALID, "invalid_validity_window")
    open_starts_on: date | None
    open_ends_on: date | None
    if re.search(r"\b(?:kadar|son\s+basvuru|bitis)\b", text):
        open_starts_on, open_ends_on = None, boundary
    elif re.search(r"\b(?:itibaren|baslangic|baslayan)\b", text):
        open_starts_on, open_ends_on = boundary, None
    else:
        return _failure(raw, NormalizationStatus.AMBIGUOUS, "validity_boundary_role_unknown")
    status = EvidenceStatus.STATED if part.year is not None else EvidenceStatus.INFERRED
    warnings = () if part.year is not None else ("year_inferred_from_reference_date",)
    return NormalizationResult(
        raw=raw,
        value=ValidityWindow(
            raw=raw,
            starts_on=open_starts_on,
            ends_on=open_ends_on,
            status=status,
        ),
        status=NormalizationStatus.NORMALIZED,
        reason_code="open_validity_normalized",
        warnings=warnings,
    )
