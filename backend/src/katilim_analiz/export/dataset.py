"""Export the latest validated campaign records as a versioned public dataset.

The TEKNOFEST delivery package requires a public dataset artifact.  This
module reads the same latest-record projection the public API serves (the
``PostgresCampaignReadAdapter`` snapshot at one explicit ``as_of`` instant),
keeps only ``RecordStatus.VALIDATED`` records, and serializes them into one
deterministic, shareable JSON document: every record carries its bank, title,
family, type, evidence-backed facts (verbatim quotes plus the official source
URL), and full extraction provenance.

The builder is pure so the serialization shape is unit-testable without a
database; only ``export_public_dataset`` touches PostgreSQL.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, field_validator
from sqlalchemy import func, select

from katilim_analiz.application.models import (
    CampaignCursor,
    CampaignListFilters,
    CampaignProjection,
)
from katilim_analiz.contracts import RecordStatus
from katilim_analiz.storage.database import Database
from katilim_analiz.storage.read_adapter import PostgresCampaignReadAdapter

DATASET_ID = "katilim-analiz-public-dataset"
DATASET_SCHEMA_VERSION = "1.0"
_PAGE_SIZE = 100
_MAX_RECORDS = 10_000

SemanticVersion = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]


class _ExportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _timezone_required(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone offset")
    return value


class PublicDatasetFact(_ExportModel):
    """One evidence-backed fact: the field it supports and its verbatim quote."""

    field_pointer: str
    quote: str
    evidence_status: str
    evidence_sha256: str


class PublicDatasetProvenance(_ExportModel):
    """Extraction provenance copied verbatim from the persisted record."""

    method: str
    extractor_version: str
    schema_version: str
    prompt_version: str | None = None
    model_id: str | None = None
    model_digest: str | None = None
    started_at: datetime
    completed_at: datetime


class PublicDatasetRecord(_ExportModel):
    """One latest validated campaign record with its source provenance."""

    campaign_key: str
    record_id: str
    version: Annotated[int, Field(ge=1)]
    bank_id: str
    bank_name: str
    title: str
    product_family: str
    campaign_type: str
    summary: str | None = None
    source_url: HttpUrl
    observed_at: datetime
    data: dict[str, Any]
    facts: list[PublicDatasetFact]
    extraction: PublicDatasetProvenance
    record_sha256: str


class PublicDataset(_ExportModel):
    """Deterministic, versioned public dataset document."""

    schema_version: Literal["1.0"] = DATASET_SCHEMA_VERSION
    dataset_id: Literal["katilim-analiz-public-dataset"] = DATASET_ID
    dataset_version: SemanticVersion
    generated_at: datetime
    record_count: Annotated[int, Field(ge=0)]
    records: list[PublicDatasetRecord]

    _validate_generated_at = field_validator("generated_at")(_timezone_required)


class PublicDatasetExportResult(_ExportModel):
    dataset_version: str
    generated_at: datetime
    record_count: int
    output_path: str
    status: Literal["exported"] = "exported"


def _record_from_projection(projection: CampaignProjection) -> PublicDatasetRecord:
    record = projection.record
    return PublicDatasetRecord(
        campaign_key=projection.campaign_key or record.id,
        record_id=record.id,
        version=record.version,
        bank_id=record.data.bank_id,
        bank_name=projection.bank_name,
        title=record.data.title,
        product_family=record.data.product_family.value,
        campaign_type=record.data.campaign_type.value,
        summary=record.data.summary,
        source_url=projection.source_url,
        observed_at=record.observed_at,
        data=record.data.model_dump(mode="json"),
        facts=[
            PublicDatasetFact(
                field_pointer=evidence.field_pointer,
                quote=evidence.quote,
                evidence_status=evidence.status.value,
                evidence_sha256=evidence.evidence_sha256,
            )
            for evidence in record.evidence
        ],
        extraction=PublicDatasetProvenance(
            method=record.extraction.method.value,
            extractor_version=record.extraction.extractor_version,
            schema_version=record.extraction.schema_version,
            prompt_version=record.extraction.prompt_version,
            model_id=record.extraction.model_id,
            model_digest=record.extraction.model_digest,
            started_at=record.extraction.started_at,
            completed_at=record.extraction.completed_at,
        ),
        record_sha256=record.record_sha256,
    )


def build_public_dataset(
    projections: list[CampaignProjection],
    *,
    dataset_version: str,
    generated_at: datetime,
) -> PublicDataset:
    """Build the deterministic public dataset from validated projections only."""

    records = sorted(
        (
            _record_from_projection(projection)
            for projection in projections
            if projection.record.status is RecordStatus.VALIDATED
        ),
        key=lambda record: (record.campaign_key, record.record_id),
    )
    return PublicDataset(
        dataset_version=dataset_version,
        generated_at=generated_at,
        record_count=len(records),
        records=records,
    )


def render_public_dataset(dataset: PublicDataset) -> str:
    """Serialize one dataset to stable, human-readable JSON with a trailing newline."""

    return dataset.model_dump_json(indent=2) + "\n"


def _write_dataset_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def export_public_dataset(
    database: Database,
    *,
    output_path: str | Path,
    dataset_version: str,
    as_of: datetime | None = None,
) -> PublicDatasetExportResult:
    """Read the latest validated records and write the shareable dataset file.

    ``as_of`` pins the snapshot instant and the dataset ``generated_at``; when
    omitted the database clock is used so the artifact never depends on the
    operator's local wall clock.
    """

    if as_of is None:
        async with database.session() as session:
            as_of = (await session.execute(select(func.now()))).scalar_one()
    else:
        _timezone_required(as_of)

    reads = PostgresCampaignReadAdapter(database.session_factory)
    projections: list[CampaignProjection] = []
    cursor: CampaignCursor | None = None
    while True:
        page = await reads.list_latest(
            filters=CampaignListFilters(),
            after=cursor,
            limit=_PAGE_SIZE,
            as_of=as_of,
        )
        projections.extend(page.items)
        if len(projections) > _MAX_RECORDS:
            raise ValueError(f"export exceeds the {_MAX_RECORDS}-record public dataset bound")
        if not page.has_more or not page.items:
            break
        last = page.items[-1]
        cursor = CampaignCursor(observed_at=last.record.observed_at, campaign_id=last.record.id)

    dataset = build_public_dataset(
        projections,
        dataset_version=dataset_version,
        generated_at=as_of,
    )
    resolved = Path(output_path)
    await asyncio.to_thread(_write_dataset_file, resolved, render_public_dataset(dataset))
    return PublicDatasetExportResult(
        dataset_version=dataset.dataset_version,
        generated_at=dataset.generated_at,
        record_count=dataset.record_count,
        output_path=str(resolved),
    )
