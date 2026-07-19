"""One reading of what a sentence says about a fee.

Both the rule extractor and the model-proposal merger have to answer the same
question, and when they answered it separately they disagreed: one learned that
a waiver sentence states no charge while the other still published the ceiling
inside it as the amount charged. The reading lives here so that a correction
reaches every caller.
"""

from __future__ import annotations

import re

from katilim_analiz.contracts import EvidenceStatus, FeeBasis, FeeKind, FeeValue, MoneyValue
from katilim_analiz.domain.normalization import normalize_money, normalize_rate

# The stem carries case and derivation suffixes in Turkish, so the marker ends at
# the stem and lets the suffix run on; a trailing word boundary would miss both
# the possessive and the "-siz" derivation that states the fee does not apply.
FEE_MARKER = re.compile(r"\b(?:ücret|ucret|masraf|tahsis|aidat)", re.I)
#: Phrases stating the fee is not charged, including the negated verb forms that
#: campaign pages actually use and the "covered by the bank" wording.
FEE_WAIVER_MARKER = re.compile(
    r"\b(?:ücretsiz|ucretsiz|masrafsız|masrafsiz)\b"
    r"|\b(?:alınma|alinma|alınmıyor|alinmiyor|yansıtılma|yansitilma)\w*"
    r"|\b(?:tahsil\s+edilme|talep\s+edilme)\w*"
    r"|\b(?:banka\s+tarafından\s+karşılan|banka\s+tarafindan\s+karsilan)\w*"
    r"|\b(?:yoktur|bulunmamaktadır|bulunmamaktadir)\b",
    re.I,
)
#: A waiver that only holds up to a stated ceiling, as in "50.000 TL'ye kadar".
_WAIVER_LIMIT_SPLIT = re.compile(r"\bkadar\b", re.I)


def _waiver_ceiling(text: str) -> MoneyValue | None:
    """Read the ceiling a waiver is capped at, when the sentence states one.

    The figure is stated ahead of "kadar", and it is read from that fragment
    alone: the whole sentence carries the waiver wording, which the money
    normalizer refuses to price, so passing it in would lose the ceiling.
    """

    head = _WAIVER_LIMIT_SPLIT.split(text, maxsplit=1)
    if len(head) < 2:
        return None
    return normalize_money(head[0]).value


def classify_fee(lowered: str) -> tuple[FeeKind, FeeBasis]:
    """Name the fee and the period it is charged over."""

    if "tahsis" in lowered:
        return FeeKind.ALLOCATION, FeeBasis.ONE_TIME
    if "aidat" in lowered or "yıllık" in lowered or "yillik" in lowered:
        return FeeKind.ANNUAL, FeeBasis.PER_YEAR
    if "aylık" in lowered or "aylik" in lowered:
        return FeeKind.MONTHLY, FeeBasis.PER_MONTH
    if "işlem" in lowered or "islem" in lowered:
        return FeeKind.TRANSACTION, FeeBasis.PER_TRANSACTION
    return FeeKind.OTHER, FeeBasis.ONE_TIME


def read_fee(text: str, *, status: EvidenceStatus | None = None) -> FeeValue | None:
    """Read the fee a sentence states, or ``None`` when it states none.

    A waiver is decided before any amount is read. A sentence waiving the fee up
    to 50.000 TL carries a figure that bounds the waiver, and reading that figure
    as the fee would publish the exact opposite of what the source says.
    """

    lowered = text.casefold()
    if FEE_MARKER.search(lowered) is None:
        return None
    kind, basis = classify_fee(lowered)
    extra = {} if status is None else {"status": status}

    if FEE_WAIVER_MARKER.search(lowered) is not None:
        return FeeValue(
            raw=text,
            kind=kind,
            basis=basis,
            description=text,
            waived=True,
            waiver_limit=_waiver_ceiling(text),
            **extra,
        )
    money = normalize_money(text)
    if money.value is not None:
        return FeeValue(raw=text, money=money.value, kind=kind, basis=basis, **extra)
    rate = normalize_rate(text)
    if rate.value is not None:
        return FeeValue(
            raw=text,
            rate=rate.value,
            kind=kind,
            basis=FeeBasis.PERCENT_OF_AMOUNT,
            **extra,
        )
    return None
