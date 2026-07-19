"""Narrow application ports implemented by PostgreSQL and optional local-model adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from katilim_analiz.application.health import MigrationRevisions
from katilim_analiz.application.models import (
    CampaignCursor,
    CampaignListFilters,
    CampaignProjection,
    CampaignReadSlice,
    NotificationCursor,
    NotificationReadSlice,
    PreviewExtractionOutput,
)
from katilim_analiz.application.processing import (
    CleanResult,
    CollectionFetch,
    ExtractionProcessResult,
    PersistenceBundle,
    PersistenceResult,
    ProcessSourceResult,
    SourceRequest,
)
from katilim_analiz.contracts import ChatQueryPlan, CleanDocument, CoverageEntry


class CampaignReadPort(Protocol):
    """Read model contract. Implementations select latest logical versions as-of."""

    async def list_latest(
        self,
        *,
        filters: CampaignListFilters,
        after: CampaignCursor | None,
        limit: int,
        as_of: datetime,
    ) -> CampaignReadSlice: ...

    async def get(self, campaign_id: str, *, as_of: datetime) -> CampaignProjection | None: ...

    async def get_many(
        self,
        campaign_ids: list[str],
        *,
        as_of: datetime,
    ) -> list[CampaignProjection]: ...

    async def search(
        self,
        plan: ChatQueryPlan,
        *,
        as_of: datetime,
    ) -> list[CampaignProjection]: ...

    async def latest_coverage(self, *, as_of: datetime) -> list[CoverageEntry]: ...


class NotificationReadPort(Protocol):
    """Read-only campaign-change feed; no acknowledgement exists before V1.2."""

    async def list_notifications(
        self,
        *,
        after: NotificationCursor | None,
        limit: int,
    ) -> NotificationReadSlice: ...


class DashboardReadPort(CampaignReadPort, NotificationReadPort, Protocol):
    """Combined production read adapter used by the campaign presentation service."""


class DatabaseHealthPort(Protocol):
    async def ping(self) -> bool: ...

    async def migration_revisions(self) -> MigrationRevisions: ...


class ModelHealthPort(Protocol):
    async def ping(self) -> bool: ...


class ChatPlanCandidatePort(Protocol):
    """A model may propose only a typed plan and receives no execution authority."""

    async def propose(self, question: str) -> ChatQueryPlan | None: ...


class CollectionPort(Protocol):
    async def fetch(self, source: SourceRequest) -> CollectionFetch: ...

    async def clean(self, fetched: CollectionFetch) -> CleanResult: ...


class ExtractionPort(Protocol):
    async def extract_and_validate(
        self,
        source: SourceRequest,
        cleaned: CleanDocument,
    ) -> ExtractionProcessResult: ...


class PreviewExtractionPort(Protocol):
    """Run the configured extractor without persistence or validation authority."""

    async def extract(self, document: CleanDocument) -> PreviewExtractionOutput: ...


class CampaignWritePort(Protocol):
    """Persist one bundle transactionally and report canonical counts/idempotency.

    The BDDK registry must already have seeded the source row. Implementations fail
    closed when bank identity, name, or approved source host differs; they never
    synthesize registry version, listing order, hosts, or digital-bank metadata.
    Coverage counts are derived from committed PostgreSQL facts, not caller guesses.
    """

    async def persist(self, bundle: PersistenceBundle) -> PersistenceResult: ...


class JobOutcomePort(Protocol):
    async def record(self, result: ProcessSourceResult) -> None: ...
