"""Readiness orchestration independent of database/model implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from katilim_analiz.application.models import HealthChecks, HealthResponse

if TYPE_CHECKING:
    from katilim_analiz.application.ports import DatabaseHealthPort, ModelHealthPort


@dataclass(frozen=True, slots=True)
class MigrationRevisions:
    current: str | None
    head: str

    def __post_init__(self) -> None:
        if not self.head.strip():
            raise ValueError("migration head revision must not be empty")
        if self.current is not None and not self.current.strip():
            raise ValueError("current migration revision must be null or non-empty")


class ReadinessService:
    def __init__(
        self,
        *,
        database: DatabaseHealthPort,
        model: ModelHealthPort | None,
        model_required: bool,
    ) -> None:
        self._database = database
        self._model = model
        self._model_required = model_required

    async def check(self) -> HealthResponse:
        try:
            database_ok = await self._database.ping()
        except Exception:  # adapter failures are readiness state, not request crashes
            database_ok = False

        migration_status: Literal["ok", "failed", "out_of_date"] = "failed"
        if database_ok:
            try:
                revisions = await self._database.migration_revisions()
                migration_status = (
                    "ok"
                    if revisions.current is not None and revisions.current == revisions.head
                    else "out_of_date"
                )
            except Exception:
                migration_status = "failed"

        model_status: Literal["ok", "failed", "not_required"] = "not_required"
        if self._model_required:
            try:
                model_status = (
                    "ok" if self._model is not None and await self._model.ping() else "failed"
                )
            except Exception:
                model_status = "failed"

        ready = (
            database_ok
            and migration_status == "ok"
            and model_status
            in {
                "ok",
                "not_required",
            }
        )
        return HealthResponse(
            status="ok" if ready else "degraded",
            checks=HealthChecks(
                database="ok" if database_ok else "failed",
                migration=migration_status,
                model=model_status,
            ),
        )
