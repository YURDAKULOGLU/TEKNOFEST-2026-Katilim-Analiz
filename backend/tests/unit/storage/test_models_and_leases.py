from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from katilim_analiz.storage.base import Base
from katilim_analiz.storage.repositories import JobRepository, OutboxRepository


class _EmptyResult:
    def scalar_one_or_none(self) -> None:
        return None


class _CapturingSession:
    def __init__(self) -> None:
        self.statement: Any = None

    async def execute(self, statement: Any) -> _EmptyResult:
        self.statement = statement
        return _EmptyResult()


@pytest.mark.asyncio
async def test_job_claim_uses_for_update_skip_locked() -> None:
    session = _CapturingSession()
    repository = JobRepository(session)  # type: ignore[arg-type]

    assert await repository.claim_next("worker-1") is None
    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert "durable_jobs.lease_expires_at <= now()" in compiled


@pytest.mark.asyncio
async def test_outbox_claim_uses_for_update_skip_locked() -> None:
    session = _CapturingSession()
    repository = OutboxRepository(session)  # type: ignore[arg-type]

    assert await repository.claim_next("publisher-1") is None
    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert "outbox_events.published_at IS NULL" in compiled


@pytest.mark.asyncio
async def test_worker_lease_duration_is_bounded() -> None:
    session = _CapturingSession()
    repository = JobRepository(session)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="at most one day"):
        await repository.claim_next("worker-1", lease_for=timedelta(days=2))


def test_metadata_contains_complete_storage_and_auth_seams() -> None:
    expected = {
        "sources",
        "fetch_artifacts",
        "clean_documents",
        "source_blocks",
        "extraction_candidates",
        "evidence_refs",
        "campaign_records",
        "campaign_observations",
        "monitored_campaign_targets",
        "monitored_source_states",
        "coverage_entries",
        "durable_jobs",
        "outbox_events",
        "comparison_snapshots",
        "auth_users",
        "auth_sessions",
    }

    assert expected <= set(Base.metadata.tables)
    assert (
        Base.metadata.tables["campaign_records"].c.rate_min.type.python_type.__name__ == "Decimal"
    )
    assert Base.metadata.tables["campaign_records"].c.data.type.__class__.__name__ == "JSONB"
