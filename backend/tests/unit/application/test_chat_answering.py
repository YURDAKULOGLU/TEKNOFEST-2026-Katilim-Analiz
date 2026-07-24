"""Issue #16 regression suite: the two live questions, composition and the gate."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from katilim_analiz.application.answering import ComposerFact, answer_is_grounded
from katilim_analiz.application.models import CampaignProjection
from katilim_analiz.application.services import ChatService
from katilim_analiz.contracts import (
    CampaignData,
    CampaignRecord,
    CampaignType,
    ChatQueryPlan,
    ChatRequest,
    ComparisonContext,
    EvidenceRef,
    EvidenceStatus,
    ExtractionMetadata,
    ExtractionMethod,
    ProductFamily,
    QueryIntent,
    RateKind,
    RatePeriod,
    RateValue,
    RecordStatus,
    SalesChannel,
    TermRange,
    ValidityWindow,
)
from katilim_analiz.domain.normalization import canonicalize_turkish_text

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
SHA = "0" * 64

QUESTION_BEST_RATE = "Taşıt finansmanında en uygun kâr payı oranı hangi bankada?"
QUESTION_VAKIF_TERM = "Vakıf Katılım taşıt finansmanında vade kaç ay?"


def _evidence(identifier: str, pointer: str, quote: str) -> EvidenceRef:
    return EvidenceRef(
        id=f"evidence:{identifier}:{pointer.rsplit('/', 1)[-1]}",
        field_pointer=pointer,
        source_document_id=f"doc:{identifier}",
        block_id=f"block:{identifier}",
        quote=quote,
        start_char=0,
        end_char=len(quote),
        evidence_sha256=SHA,
        status=EvidenceStatus.STATED,
    )


def financing_projection(
    identifier: str,
    *,
    bank_id: str,
    bank_name: str,
    title: str = "Taşıt Finansmanı",
    rate_raw: str = "%3,5",
    rate_value: str = "3.5",
    rate_period: RatePeriod = RatePeriod.MONTHLY,
    term_months: int = 48,
    campaign_type: CampaignType = CampaignType.INSTALLMENT,
    status: RecordStatus = RecordStatus.VALIDATED,
) -> CampaignProjection:
    rate_quote = f"Aylık kâr payı oranı {rate_raw} olarak uygulanır."
    term_quote = f"Vade {term_months} aya kadar uzatılabilir."
    record = CampaignRecord(
        id=identifier,
        version=1,
        source_document_id=f"doc:{identifier}",
        observed_at=NOW,
        data=CampaignData(
            bank_id=bank_id,
            title=title,
            product_family=ProductFamily.FINANCING,
            campaign_type=campaign_type,
            rates=[
                RateValue(
                    raw=rate_raw,
                    value_percent=Decimal(rate_value),
                    kind=RateKind.FINANCING_PROFIT_RATE,
                    period=rate_period,
                    # Fixed pricing-term context so two banks' rates share one
                    # comparison signature even when their maximum terms differ.
                    term_months=12,
                    basis_label="nominal",
                )
            ],
            terms=[TermRange(raw=f"{term_months} aya kadar", maximum_months=term_months)],
            validity=ValidityWindow(
                raw="2026", starts_on=date(2026, 1, 1), ends_on=date(2026, 12, 31)
            ),
            customer_segments=["Bireysel"],
            comparison_context=ComparisonContext(
                product_currency="TRY",
                customer_segment_keys=["retail"],
                sales_channel=SalesChannel.ALL,
                new_customer_only=False,
                product_mechanism="murabaha",
                secured=True,
            ),
        ),
        evidence=[
            _evidence(identifier, "/data/rates/0/value_percent", rate_quote),
            _evidence(identifier, "/data/terms/0/maximum_months", term_quote),
        ],
        extraction=ExtractionMetadata(
            method=ExtractionMethod.RULE,
            extractor_version="test",
            schema_version="1",
            started_at=NOW,
            completed_at=NOW,
        ),
        status=status,
        record_sha256=SHA,
    )
    return CampaignProjection(
        record=record,
        campaign_key=f"{bank_id}:{identifier}",
        bank_name=bank_name,
        source_url=f"https://example.test/{identifier}",
        source_title=f"Kaynak {identifier}",
    )


VAKIF = financing_projection(
    "vakif-tasit",
    bank_id="vakif-katilim",
    bank_name="VAKIF KATILIM BANKASI A.Ş.",
    rate_raw="%3,5",
    rate_value="3.5",
    term_months=48,
)
ZIRAAT = financing_projection(
    "ziraat-tasit",
    bank_id="ziraat-katilim",
    bank_name="ZİRAAT KATILIM BANKASI A.Ş.",
    rate_raw="%4,2",
    rate_value="4.2",
    term_months=36,
)


class StrictSearchReads:
    """Emulates the PostgreSQL adapter: AND keyword semantics + typed filters.

    This reproduces the production failure mode: an over-constrained plan
    (residual keywords, inferred campaign type) returns zero rows even though
    validated records exist, so the service-level relaxation ladder and the
    in-process relevance filter are exercised for real.
    """

    def __init__(self, items: list[CampaignProjection]) -> None:
        self.items = items
        self.plans: list[ChatQueryPlan] = []

    async def search(self, plan: ChatQueryPlan, *, as_of: datetime) -> list[CampaignProjection]:
        self.plans.append(plan)
        results = []
        for item in self.items:
            data = item.record.data
            if plan.bank_ids and data.bank_id not in plan.bank_ids:
                continue
            if plan.product_family is not None and data.product_family is not plan.product_family:
                continue
            if plan.campaign_type is not None and data.campaign_type is not plan.campaign_type:
                continue
            if plan.keywords:
                indexed = set(
                    canonicalize_turkish_text(f"{data.title} {data.summary or ''}").split()
                )
                if not all(
                    canonicalize_turkish_text(keyword) in indexed for keyword in plan.keywords
                ):
                    continue
            results.append(item)
        return results[: plan.limit]


class FixedComposer:
    def __init__(self, answer: str | None, *, error: bool = False) -> None:
        self._answer = answer
        self._error = error
        self.calls = 0

    async def compose(self, question: str, facts: Sequence[ComposerFact]) -> str | None:
        self.calls += 1
        if self._error:
            raise RuntimeError("model is down")
        return self._answer


async def test_best_rate_question_reaches_the_validated_financing_record() -> None:
    """Live question 1 must surface vakif's validated %3,5 record, not abstain."""

    reads = StrictSearchReads([VAKIF])
    answer = await ChatService(reads, clock=lambda: NOW).answer(
        ChatRequest(question=QUESTION_BEST_RATE, as_of=NOW)
    )

    assert answer.insufficient_evidence is False
    assert answer.plan.intent is QueryIntent.COMPARE
    assert answer.plan.campaign_type is None
    assert "%3,5" in answer.answer
    assert "VAKIF KATILIM" in answer.answer
    assert any("%3,5" in citation.quote for citation in answer.citations)
    # Only one bank is validated: no winner may be declared.
    assert "daha avantajlı" not in answer.answer
    assert any("karşılaştırma" in warning.lower() for warning in answer.warnings)


async def test_vakif_term_question_answers_with_months_from_the_vakif_record() -> None:
    """Live question 2 must answer 48 ay from the vakif record only."""

    reads = StrictSearchReads([ZIRAAT, VAKIF])
    answer = await ChatService(reads, clock=lambda: NOW).answer(
        ChatRequest(question=QUESTION_VAKIF_TERM, as_of=NOW)
    )

    assert answer.insufficient_evidence is False
    assert "48 ay" in answer.answer
    assert "VAKIF KATILIM" in answer.answer
    # The named bank wins: the other bank's term must not leak into the answer.
    assert "ZİRAAT" not in answer.answer
    assert "36" not in answer.answer
    assert all("36" not in citation.quote for citation in answer.citations)


async def test_two_comparable_banks_produce_a_ranked_side_by_side_answer() -> None:
    reads = StrictSearchReads([ZIRAAT, VAKIF])
    answer = await ChatService(reads, clock=lambda: NOW).answer(
        ChatRequest(question=QUESTION_BEST_RATE, as_of=NOW)
    )

    assert answer.insufficient_evidence is False
    # Same-kind monthly rates genuinely rank: the lower rate wins that dimension.
    assert "Kâr payı oranı açısından VAKIF KATILIM BANKASI A.Ş. daha avantajlıdır" in answer.answer
    assert "%3,5" in answer.answer
    assert "%4,2" in answer.answer


async def test_incomparable_bases_present_values_side_by_side_without_a_winner() -> None:
    annual = financing_projection(
        "ziraat-tasit-yillik",
        bank_id="ziraat-katilim",
        bank_name="ZİRAAT KATILIM BANKASI A.Ş.",
        rate_raw="%42",
        rate_value="42",
        rate_period=RatePeriod.ANNUAL,
        term_months=48,
    )
    reads = StrictSearchReads([annual, VAKIF])
    answer = await ChatService(reads, clock=lambda: NOW).answer(
        ChatRequest(question=QUESTION_BEST_RATE, as_of=NOW)
    )

    assert answer.insufficient_evidence is False
    assert (
        "daha avantajlı" not in answer.answer.replace("Vade açısından", "")
        or "Kâr payı oranı açısından" not in answer.answer
    )
    assert "%3,5" in answer.answer
    assert "%42" in answer.answer
    assert "sıralanamadı" in answer.answer or any(
        "sıralama yapılmadı" in warning for warning in answer.warnings
    )


async def test_noise_keyword_does_not_veto_a_bank_scoped_question() -> None:
    """Live battery regression: "azami" appears in no evidence, yet the bank,
    family, and dimension filters already pin the answer to the vakif record."""

    reads = StrictSearchReads([ZIRAAT, VAKIF])
    answer = await ChatService(reads, clock=lambda: NOW).answer(
        ChatRequest(
            question="Vakıf Katılım'ın taşıt finansmanında azami vade kaç ay?",
            as_of=NOW,
        )
    )

    assert answer.insufficient_evidence is False
    assert "48 ay" in answer.answer
    assert "VAKIF KATILIM" in answer.answer
    assert "ZİRAAT" not in answer.answer


async def test_term_specific_rate_question_selects_the_matching_table_row() -> None:
    """"36 ay vadede oran?" must answer %3.40@36, not the 12-month headline.

    The per-term rates are table-bound (INFERRED with verbatim cell quotes),
    exercising the inferred-evidence fallback of the rate fact."""

    rates = [
        RateValue(
            raw="%3.50",
            value_percent=Decimal("3.50"),
            kind=RateKind.FINANCING_PROFIT_RATE,
            period=RatePeriod.MONTHLY,
            term_months=12,
        ),
        RateValue(
            raw="%3.40",
            value_percent=Decimal("3.40"),
            kind=RateKind.FINANCING_PROFIT_RATE,
            period=RatePeriod.MONTHLY,
            term_months=36,
        ),
    ]
    evidence = [
        _evidence("vakif-tasit-tablo", "/data/rates/0/value_percent", "%3.50").model_copy(
            update={"status": EvidenceStatus.INFERRED}
        ),
        _evidence("vakif-tasit-tablo", "/data/rates/1/value_percent", "%3.40").model_copy(
            update={"id": "evidence:vakif-tasit-tablo:rate-36", "status": EvidenceStatus.INFERRED}
        ),
        _evidence("vakif-tasit-tablo", "/data/terms/0/maximum_months", "48 aya varan vade"),
    ]
    base = financing_projection(
        "vakif-tasit-tablo",
        bank_id="vakif-katilim",
        bank_name="VAKIF KATILIM BANKASI A.Ş.",
    )
    projection = base.model_copy(
        update={
            "record": base.record.model_copy(
                update={
                    "data": base.record.data.model_copy(update={"rates": rates}),
                    "evidence": evidence,
                }
            )
        }
    )

    reads = StrictSearchReads([projection])
    answer = await ChatService(reads, clock=lambda: NOW).answer(
        ChatRequest(
            question="Vakıf Katılım taşıt finansmanında 36 ay vadede oran yüzde kaç?",
            as_of=NOW,
        )
    )

    assert answer.insufficient_evidence is False
    assert "36 ay vadede kâr payı oranı %3.40" in answer.answer
    assert any("%3.40" in citation.quote for citation in answer.citations)


async def test_subfamily_word_keeps_the_answer_on_the_matching_product_sheet() -> None:
    """A tasit question must not be answered with an ihtiyac sheet's numbers."""

    ihtiyac = financing_projection(
        "emlak-ihtiyac",
        bank_id="emlak-katilim",
        bank_name="TÜRKİYE EMLAK KATILIM BANKASI A.Ş.",
        title="İhtiyaç Finansmanı",
        rate_raw="%1,69",
        rate_value="1.69",
        term_months=12,
    )
    reads = StrictSearchReads([ihtiyac, VAKIF])
    answer = await ChatService(reads, clock=lambda: NOW).answer(
        ChatRequest(
            question="Taşıt finansmanında hangi bankanın kâr payı oranı daha düşük?",
            as_of=NOW,
        )
    )

    assert answer.insufficient_evidence is False
    assert "VAKIF KATILIM" in answer.answer
    assert "%1,69" not in answer.answer
    assert any("bir bankanın doğrulanmış kaydı" in warning for warning in answer.warnings)


async def test_absent_fee_question_states_absence_and_never_reaches_the_composer() -> None:
    """Live battery regression: the emlak record states no fee, and the model
    answered the fee question with the financing amount (30.000 TL). The
    absent dimension must be named explicitly and the composer skipped —
    the grounding gate checks number presence, not attribution."""

    composer = FixedComposer("Tahsis ücreti 30.000,00 TL ödenmektedir.")
    reads = StrictSearchReads([VAKIF])
    answer = await ChatService(reads, composer=composer, clock=lambda: NOW).answer(
        ChatRequest(
            question="Vakıf Katılım taşıt finansmanında tahsis ücreti ne kadar?",
            as_of=NOW,
        )
    )

    assert answer.insufficient_evidence is False
    assert "ücret bilgisi kaynak kanıtıyla yer almıyor" in answer.answer
    assert "30.000" not in answer.answer
    assert composer.calls == 0


async def test_abstention_is_unchanged_when_nothing_matches() -> None:
    reads = StrictSearchReads([])
    answer = await ChatService(reads, clock=lambda: NOW).answer(
        ChatRequest(question=QUESTION_VAKIF_TERM, as_of=NOW)
    )

    assert answer.insufficient_evidence is True
    assert answer.citations == []
    assert answer.answer == (
        "Bu soruyu yanıtlamak için yeterli doğrulanmış kaynak kanıtı bulunamadı."
    )


async def test_needs_review_records_never_answer() -> None:
    reads = StrictSearchReads(
        [
            financing_projection(
                "vakif-tasit-review",
                bank_id="vakif-katilim",
                bank_name="VAKIF KATILIM BANKASI A.Ş.",
                status=RecordStatus.NEEDS_REVIEW,
            )
        ]
    )
    answer = await ChatService(reads, clock=lambda: NOW).answer(
        ChatRequest(question=QUESTION_VAKIF_TERM, as_of=NOW)
    )

    assert answer.insufficient_evidence is True


async def test_grounded_model_answer_replaces_the_template() -> None:
    composed = (
        "Vakıf Katılım Taşıt Finansmanı ürününde kâr payı oranı %3,5 olarak "
        "sunulmakta ve 48 aya kadar vade imkânı bulunmaktadır."
    )
    composer = FixedComposer(composed)
    reads = StrictSearchReads([VAKIF])
    answer = await ChatService(reads, composer=composer, clock=lambda: NOW).answer(
        ChatRequest(question=QUESTION_VAKIF_TERM, as_of=NOW)
    )

    assert composer.calls == 1
    assert answer.answer == composed
    assert answer.insufficient_evidence is False
    assert answer.citations


async def test_poisoned_model_number_is_rejected_and_template_serves() -> None:
    composer = FixedComposer(
        "Vakıf Katılım Taşıt Finansmanı ürününde kâr payı oranı %2,9 olarak sunulmaktadır."
    )
    reads = StrictSearchReads([VAKIF])
    answer = await ChatService(reads, composer=composer, clock=lambda: NOW).answer(
        ChatRequest(question=QUESTION_VAKIF_TERM, as_of=NOW)
    )

    assert "%2,9" not in answer.answer
    assert "%3,5" in answer.answer
    assert any("kanıt kapısından geçemedi" in warning for warning in answer.warnings)


async def test_model_failure_still_serves_the_template_answer() -> None:
    composer = FixedComposer(None, error=True)
    reads = StrictSearchReads([VAKIF])
    answer = await ChatService(reads, composer=composer, clock=lambda: NOW).answer(
        ChatRequest(question=QUESTION_VAKIF_TERM, as_of=NOW)
    )

    assert answer.insufficient_evidence is False
    assert "48 ay" in answer.answer
    assert answer.citations


async def test_compare_question_never_reaches_the_composer() -> None:
    """Live battery regression: for COMPARE questions the model could phrase or
    invent a winner claim ("bank X is better") that the number gate cannot
    see. Ranking wording must always come from the deterministic verdict
    template, so the composer is never called for COMPARE intent."""

    composer = FixedComposer("ZİRAAT KATILIM daha avantajlıdır.")
    reads = StrictSearchReads([ZIRAAT, VAKIF])
    answer = await ChatService(reads, composer=composer, clock=lambda: NOW).answer(
        ChatRequest(question=QUESTION_BEST_RATE, as_of=NOW)
    )

    assert composer.calls == 0
    assert "ZİRAAT KATILIM daha avantajlıdır" not in answer.answer
    assert "Kâr payı oranı açısından VAKIF KATILIM BANKASI A.Ş. daha avantajlıdır" in answer.answer
    assert "%3,5" in answer.answer
    assert "%4,2" in answer.answer


def test_number_gate_normalizes_decimal_separator_only() -> None:
    facts = (
        ComposerFact(
            bank_name="VAKIF KATILIM BANKASI A.Ş.",
            product_title="Taşıt Finansmanı",
            label="Kâr payı oranı",
            value="%3,5",
            quote="Aylık kâr payı oranı %3,5 olarak uygulanır.",
        ),
    )

    assert answer_is_grounded("Oran %3.5 düzeyindedir.", facts)
    assert answer_is_grounded("Oran %3,5 düzeyindedir.", facts)
    assert not answer_is_grounded("Oran %3,50 düzeyindedir.", facts)
    assert not answer_is_grounded("Oran %3,5, vade 120 aydır.", facts)
    assert answer_is_grounded("Sayı içermeyen cevap.", facts)


def test_number_gate_rejects_a_unit_swap_on_a_grounded_number() -> None:
    """Live battery regression: evidence stating "%3,50" grounded a composed
    answer saying "3,50 TL" because the gate only checked number presence.
    A unit-annotated number must be grounded with a compatible unit."""

    facts = (
        ComposerFact(
            bank_name="VAKIF KATILIM BANKASI A.Ş.",
            product_title="Taşıt Finansmanı",
            label="Kâr payı oranı",
            value="%3,5",
            quote="Aylık kâr payı oranı %3,5 olarak uygulanır.",
        ),
    )

    # Percent evidence must not ground a currency claim of the same number.
    assert not answer_is_grounded("Oran 3,5 TL olarak uygulanır.", facts)
    assert not answer_is_grounded("Oran 3.5 lira olarak uygulanır.", facts)
    assert not answer_is_grounded("Oran ₺3,5 olarak uygulanır.", facts)
    # The same number with the same unit class stays grounded.
    assert answer_is_grounded("Oran %3,5 olarak uygulanır.", facts)
    assert answer_is_grounded("Oran yüzde 3,5 olarak uygulanır.", facts)
    # A number with no discernible unit keeps presence-only behavior.
    assert answer_is_grounded("Oran 3,5 olarak uygulanır.", facts)


def test_number_gate_rejects_months_recast_as_currency() -> None:
    facts = (
        ComposerFact(
            bank_name="VAKIF KATILIM BANKASI A.Ş.",
            product_title="Taşıt Finansmanı",
            label="Azami vade",
            value="48 ay",
            quote="Vade 48 aya kadar uzatılabilir.",
        ),
    )

    assert not answer_is_grounded("Ücret 48 TL olarak belirtilmiştir.", facts)
    assert not answer_is_grounded("Oran %48 düzeyindedir.", facts)
    assert answer_is_grounded("Vade 48 ay olarak sunulmaktadır.", facts)
    assert answer_is_grounded("Vade 48 aya kadar çıkabilmektedir.", facts)
    assert answer_is_grounded("Vade değeri 48 olarak belirtilmiştir.", facts)
