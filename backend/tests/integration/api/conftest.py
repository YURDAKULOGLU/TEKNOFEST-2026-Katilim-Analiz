from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio

from katilim_analiz.api.app import create_app
from katilim_analiz.application.container import ApplicationContainer
from katilim_analiz.application.health import MigrationRevisions, ReadinessService
from katilim_analiz.application.models import (
    CampaignFacets,
    CampaignProjection,
    CampaignReadSlice,
    FacetOption,
    NotificationCursor,
    NotificationReadItem,
    NotificationReadSlice,
)
from katilim_analiz.application.preview import ExtractionPreviewService
from katilim_analiz.application.services import CampaignService, ChatService
from katilim_analiz.config import ModelProfile, Settings
from katilim_analiz.contracts import (
    CampaignChangeEvent,
    CampaignData,
    CampaignRecord,
    CampaignType,
    ComparisonContext,
    CoverageEntry,
    CoverageStatus,
    EvidenceRef,
    EvidenceStatus,
    ExtractionMetadata,
    ExtractionMethod,
    ProductFamily,
    RateKind,
    RatePeriod,
    RateValue,
    RecordStatus,
    SalesChannel,
    ValidityWindow,
)
from katilim_analiz.extraction import ExtractionPipeline
from katilim_analiz.runtime.adapters import PipelinePreviewAdapter
from katilim_analiz.runtime.registry import load_runtime_registry

NOW = datetime(2026, 7, 18, 18, 30, tzinfo=UTC)
SHA = "0" * 64


def make_projection(
    identifier: str,
    *,
    bank_id: str,
    bank_name: str,
    family: ProductFamily = ProductFamily.FINANCING,
    rate: str = "1.50",
) -> CampaignProjection:
    quote = f"Aylık kâr oranı %{rate}"
    evidence = EvidenceRef(
        id=f"evidence:{identifier}",
        field_pointer="/data/rates/0/value_percent",
        source_document_id=f"doc:{identifier}",
        block_id=f"block:{identifier}",
        quote=quote,
        start_char=0,
        end_char=len(quote),
        evidence_sha256=SHA,
        status=EvidenceStatus.STATED,
    )
    return CampaignProjection(
        record=CampaignRecord(
            id=identifier,
            version=1,
            source_document_id=f"doc:{identifier}",
            observed_at=NOW,
            data=CampaignData(
                bank_id=bank_id,
                title=f"Kampanya {identifier}",
                summary="Doğrulanmış test kampanyası",
                product_family=family,
                campaign_type=CampaignType.FINANCING_RATE,
                rates=[
                    RateValue(
                        raw=f"%{rate}",
                        value_percent=Decimal(rate),
                        kind=RateKind.FINANCING_PROFIT_RATE,
                        period=RatePeriod.MONTHLY,
                        term_months=12,
                        basis_label="nominal",
                    )
                ],
                validity=ValidityWindow(
                    raw="2026",
                    starts_on=date(2026, 1, 1),
                    ends_on=date(2026, 12, 31),
                ),
                customer_segments=["Bireysel"],
                comparison_context=ComparisonContext(
                    product_currency="TRY",
                    customer_segment_keys=["retail"],
                    sales_channel=SalesChannel.ALL,
                    new_customer_only=False,
                    product_mechanism="standard",
                    secured=False,
                ),
            ),
            evidence=[evidence],
            extraction=ExtractionMetadata(
                method=ExtractionMethod.HYBRID,
                extractor_version="test",
                schema_version="1",
                model_id="test-model",
                started_at=NOW,
                completed_at=NOW,
            ),
            status=RecordStatus.VALIDATED,
            record_sha256=SHA,
        ),
        campaign_key=f"{bank_id}:{identifier}",
        bank_name=bank_name,
        source_url=f"https://example.test/{identifier}",
        source_title=f"Kaynak {identifier}",
    )


class FakeReads:
    def __init__(self) -> None:
        self.items = [
            make_projection("campaign-a", bank_id="bank-a", bank_name="Banka A", rate="1.20"),
            make_projection("campaign-b", bank_id="bank-b", bank_name="Banka B", rate="1.80"),
        ]
        self.raise_error = False
        self.notification_error = False
        self.notifications: list[CampaignChangeEvent] = []
        self.last_query = None

    def _check(self) -> None:
        if self.raise_error:
            raise RuntimeError("database-password-must-not-leak")

    async def list_latest(self, *, filters, after, limit, as_of):  # type: ignore[no-untyped-def]
        self._check()
        self.last_query = (filters, after, limit, as_of)
        selected = [
            item for item in self.items if filters.bank_id in (None, item.record.data.bank_id)
        ]
        return CampaignReadSlice(
            items=selected[:limit],
            has_more=len(selected) > limit,
            facets=CampaignFacets(
                banks=[
                    FacetOption(value="bank-a", label="Banka A", count=1),
                    FacetOption(value="bank-b", label="Banka B", count=1),
                ],
                product_families=[FacetOption(value="financing", label="financing", count=2)],
                sales_channels=[FacetOption(value="all", label="all", count=2)],
            ),
        )

    async def get(self, campaign_id, *, as_of):  # type: ignore[no-untyped-def]
        self._check()
        return next((item for item in self.items if item.record.id == campaign_id), None)

    async def get_many(self, campaign_ids, *, as_of):  # type: ignore[no-untyped-def]
        self._check()
        wanted = set(campaign_ids)
        return [item for item in self.items if item.record.id in wanted]

    async def search(self, plan, *, as_of):  # type: ignore[no-untyped-def]
        self._check()
        return self.items[: plan.limit]

    async def latest_coverage(self, *, as_of):  # type: ignore[no-untyped-def]
        self._check()
        return [
            CoverageEntry(
                bank_id="bank-a",
                bank_name="Banka A",
                observed_at=NOW,
                status=CoverageStatus.SUCCESS,
                source_count=1,
                campaign_count=1,
            )
        ]

    async def list_notifications(
        self,
        *,
        after: NotificationCursor | None,
        limit: int,
    ) -> NotificationReadSlice:
        self._check()
        if self.notification_error:
            raise RuntimeError("raw-html prompt database-password-must-not-leak")
        ordered = [
            NotificationReadItem(feed_sequence=sequence, event=event)
            for sequence, event in enumerate(self.notifications, start=1)
        ]
        if after is not None:
            ordered = [item for item in ordered if item.feed_sequence > after.feed_sequence]
        return NotificationReadSlice(
            items=ordered[:limit],
        )


class FakeDatabaseHealth:
    def __init__(
        self, *, healthy: bool = True, current: str = "rev-1", head: str = "rev-1"
    ) -> None:
        self.healthy = healthy
        self.current = current
        self.head = head

    async def ping(self) -> bool:
        return self.healthy

    async def migration_revisions(self) -> MigrationRevisions:
        return MigrationRevisions(current=self.current, head=self.head)


class FakeModelHealth:
    async def ping(self) -> bool:
        return True


@pytest.fixture
def reads() -> FakeReads:
    return FakeReads()


def make_container(
    reads: FakeReads,
    *,
    database_health: FakeDatabaseHealth | None = None,
    profile: ModelProfile = ModelProfile.RULES_ONLY,
) -> ApplicationContainer:
    database_health = database_health or FakeDatabaseHealth()
    return ApplicationContainer(
        campaigns=CampaignService(reads, clock=lambda: NOW),
        chat=ChatService(reads, clock=lambda: NOW),
        readiness=ReadinessService(
            database=database_health,
            model=FakeModelHealth(),
            model_required=profile is not ModelProfile.RULES_ONLY,
        ),
        previews=ExtractionPreviewService(
            registry=load_runtime_registry(),
            extractor=PipelinePreviewAdapter(
                ExtractionPipeline(model_enabled=False, clock=lambda: NOW)
            ),
            clock=lambda: NOW,
        ),
    )


@pytest.fixture
def client_factory(reads: FakeReads) -> Callable[..., httpx.AsyncClient]:
    def factory(
        *,
        database_health: FakeDatabaseHealth | None = None,
        profile: ModelProfile = ModelProfile.RULES_ONLY,
        raise_app_exceptions: bool = True,
    ) -> httpx.AsyncClient:
        settings = Settings(
            app_allowed_hosts=["testserver"],
            app_cors_origins=["http://localhost:5173"],
            model_profile=profile,
        )
        app = create_app(
            container=make_container(reads, database_health=database_health, profile=profile),
            settings=settings,
        )
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app,
                raise_app_exceptions=raise_app_exceptions,
            ),
            base_url="http://testserver",
        )

    return factory


@pytest_asyncio.fixture
async def client(
    client_factory: Callable[..., httpx.AsyncClient],
) -> AsyncIterator[httpx.AsyncClient]:
    async with client_factory() as api_client:
        yield api_client
