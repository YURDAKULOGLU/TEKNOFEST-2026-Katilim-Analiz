from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from katilim_analiz.application.models import PreviewExtractionOutput
from katilim_analiz.application.preview import (
    ExtractionPreviewService,
    PreviewBankNotAllowedError,
)
from katilim_analiz.contracts import CleanDocument, ExtractionPreviewRequest
from katilim_analiz.extraction import ExtractionPipeline
from katilim_analiz.ingestion.registry import BankRegistry, BankSource

NOW = datetime(2026, 7, 18, 20, 30, tzinfo=UTC)


def _registry() -> BankRegistry:
    return BankRegistry(
        schema_version="1.0",
        registry_id="bddk-participation-banks",
        registry_version="2026-07-18.1",
        source_observed_on=date(2026, 7, 18),
        source_url="https://www.bddk.org.tr/Kurulus/Liste/77",
        banks=(
            BankSource(
                id="bank-a",
                listing_order=1,
                legal_name="Test Katılım A",
                listed_homepage_url="https://bank-a.example",
                allowed_hosts=("bank-a.example",),
                digital_bank=False,
            ),
        ),
    )


class CapturingPipelineAdapter:
    def __init__(self) -> None:
        self.documents: list[CleanDocument] = []
        self._pipeline = ExtractionPipeline(model_enabled=False, clock=lambda: NOW)

    async def extract(self, document: CleanDocument) -> PreviewExtractionOutput:
        self.documents.append(document)
        result = await self._pipeline.extract(document)
        return PreviewExtractionOutput(
            candidate=result.candidate,
            issues=list(result.issues),
            model_attempted=result.model_attempted,
            accepted_model_facts=result.accepted_model_facts,
        )


class MisboundHeadingAdapter(CapturingPipelineAdapter):
    async def extract(self, document: CleanDocument) -> PreviewExtractionOutput:
        result = await super().extract(document)
        assert result.candidate is not None
        candidate = result.candidate.model_copy(
            update={
                "data": result.candidate.data.model_copy(
                    update={"title": "Başka bir satırdan üretilen başlık"}
                )
            }
        )
        return result.model_copy(update={"candidate": candidate, "accepted_model_facts": 1})


@pytest.mark.asyncio
async def test_preview_builds_exact_heading_evidence_and_preserves_input_hash() -> None:
    extractor = CapturingPipelineAdapter()
    service = ExtractionPreviewService(
        registry=_registry(),
        extractor=extractor,
        clock=lambda: NOW,
    )
    text = "\n  \nKonut Finansmanı Kampanyası\n100.000 TL finansman 12 ay vadeli.\n"

    response = await service.preview(ExtractionPreviewRequest(bank_id="bank-a", text=text))

    assert response.scope == "unverified_preview"
    assert response.human_verified is False
    assert response.persisted is False
    assert response.status == "needs_review"
    assert response.input_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert response.candidate is not None
    assert response.candidate.data.title == "Konut Finansmanı Kampanyası"
    assert response.model_attempted is False
    assert response.accepted_model_facts == 0

    document = extractor.documents[0]
    assert str(document.canonical_url).startswith("https://preview.invalid/")
    assert document.blocks[0].kind == "heading"
    assert document.blocks[0].text == "Konut Finansmanı Kampanyası"
    assert document.blocks[0].locator == "text-input:line-3"
    title_evidence = next(
        evidence
        for evidence in response.candidate.evidence
        if evidence.field_pointer == "/data/title"
    )
    assert title_evidence.quote == document.blocks[0].text


def test_preview_request_rejects_whitespace_and_an_unbounded_heading() -> None:
    with pytest.raises(ValidationError):
        ExtractionPreviewRequest(bank_id="bank-a", text=" \n\t")
    with pytest.raises(ValidationError):
        ExtractionPreviewRequest(bank_id="bank-a", text="x" * 501)


@pytest.mark.asyncio
async def test_preview_abstains_when_candidate_title_is_not_the_exact_heading() -> None:
    service = ExtractionPreviewService(
        registry=_registry(),
        extractor=MisboundHeadingAdapter(),
        clock=lambda: NOW,
    )

    response = await service.preview(
        ExtractionPreviewRequest(
            bank_id="bank-a",
            text="Konut Finansmanı Kampanyası\n100.000 TL finansman 12 ay vadeli.",
        )
    )

    assert response.status == "abstained"
    assert response.candidate is None
    assert response.accepted_model_facts == 0
    assert "preview_heading_evidence_required" in response.issues


@pytest.mark.asyncio
async def test_unknown_registry_bank_is_rejected_before_extraction() -> None:
    extractor = CapturingPipelineAdapter()
    service = ExtractionPreviewService(
        registry=_registry(),
        extractor=extractor,
        clock=lambda: NOW,
    )

    with pytest.raises(PreviewBankNotAllowedError):
        await service.preview(
            ExtractionPreviewRequest(bank_id="unknown-bank", text="Kampanya başlığı")
        )

    assert extractor.documents == []
