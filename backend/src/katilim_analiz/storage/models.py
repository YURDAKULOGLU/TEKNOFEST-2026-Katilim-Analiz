"""PostgreSQL ORM models for evidence-first, versioned persistence."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from katilim_analiz.storage.base import Base


class Source(Base):
    """Approved bank source registry entry."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("listing_order > 0", name="listing_order_positive"),
        CheckConstraint("jsonb_typeof(allowed_hosts) = 'array'", name="allowed_hosts_array"),
        Index("ix_sources_registry_order", "registry_version", "listing_order", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    registry_version: Mapped[str] = mapped_column(String(32), nullable=False)
    listing_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    legal_name: Mapped[str] = mapped_column(String(500), nullable=False)
    homepage_url: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_hosts: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    digital_bank: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


Index(
    "ix_sources_legal_name_unaccent_trgm",
    func.immutable_unaccent(Source.legal_name).label("legal_name_unaccented"),
    postgresql_using="gin",
    postgresql_ops={"legal_name_unaccented": "gin_trgm_ops"},
)


class MonitoredCampaignTargetRow(Base):
    """Verified campaign detail target discovered from a monitored source index."""

    __tablename__ = "monitored_campaign_targets"
    __table_args__ = (
        CheckConstraint("last_seen_at >= first_seen_at", name="target_seen_times_ordered"),
        UniqueConstraint("bank_id", "canonical_url", name="uq_monitored_campaign_targets_bank_url"),
        UniqueConstraint("campaign_key", name="uq_monitored_campaign_targets_campaign_key"),
        Index(
            "ix_monitored_campaign_targets_active_bank_url",
            "bank_id",
            "active",
            "canonical_url",
        ),
        Index("ix_monitored_campaign_targets_discovered_from", "discovered_from"),
    )

    id: Mapped[str] = mapped_column(String(191), primary_key=True)
    bank_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    campaign_key: Mapped[str] = mapped_column(String(191), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    discovered_from: Mapped[str] = mapped_column(String(2_048), nullable=False)
    registry_version: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MonitoredSourceStateRow(Base):
    """Latest bounded observation state for one verified campaign index URL."""

    __tablename__ = "monitored_source_states"
    __table_args__ = (
        CheckConstraint(
            "last_status IN ('success','not_modified','blocked','failed')",
            name="monitored_source_status_known",
        ),
        Index("ix_monitored_source_states_observed", "last_observed_at", "bank_id"),
    )

    bank_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    index_url: Mapped[str] = mapped_column(String(2_048), primary_key=True)
    registry_version: Mapped[str] = mapped_column(String(32), nullable=False)
    last_content_sha256: Mapped[str | None] = mapped_column(String(64))
    last_status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FetchArtifactRow(Base):
    """Immutable fetch observation; raw bytes remain outside the database."""

    __tablename__ = "fetch_artifacts"
    __table_args__ = (
        CheckConstraint("raw_size_bytes >= 0", name="raw_size_nonnegative"),
        CheckConstraint(
            "status <> 'success' OR "
            "(final_url IS NOT NULL AND raw_sha256 IS NOT NULL AND raw_size_bytes > 0)",
            name="successful_fetch_has_content",
        ),
        CheckConstraint(
            "status IN ('success','not_modified','blocked','failed','skipped')",
            name="fetch_status_known",
        ),
        UniqueConstraint("artifact_sha256", name="uq_fetch_artifacts_artifact_sha256"),
        Index("ix_fetch_artifacts_source_fetched", "source_id", "fetched_at"),
        Index("ix_fetch_artifacts_raw_sha256", "raw_sha256"),
    )

    id: Mapped[str] = mapped_column(String(191), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    robots_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(500))
    etag: Mapped[str | None] = mapped_column(String(500))
    last_modified: Mapped[str | None] = mapped_column(String(500))
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    raw_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    private_raw_path: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(500))
    error_detail: Mapped[str | None] = mapped_column(String(500))
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CleanDocumentRow(Base):
    """Canonical cleaned content, deduplicated independently from fetch observations."""

    __tablename__ = "clean_documents"
    __table_args__ = (
        CheckConstraint("language IN ('tr','unknown')", name="document_language_known"),
        UniqueConstraint("clean_sha256", name="uq_clean_documents_clean_sha256"),
        Index("ix_clean_documents_source_created", "source_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(191), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    clean_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


Index(
    "ix_clean_documents_title_unaccent_trgm",
    func.immutable_unaccent(CleanDocumentRow.title).label("title_unaccented"),
    postgresql_using="gin",
    postgresql_ops={"title_unaccented": "gin_trgm_ops"},
    postgresql_where=CleanDocumentRow.title.is_not(None),
)


class FetchDocumentLink(Base):
    """Observation metadata linking a fetch event to deduplicated cleaned content."""

    __tablename__ = "fetch_document_links"
    fetch_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("fetch_artifacts.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("clean_documents.id", ondelete="CASCADE"), primary_key=True
    )
    cleaned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cleaner_version: Mapped[str] = mapped_column(String(191), nullable=False)


class SourceBlockRow(Base):
    """Evidence-addressable immutable text block."""

    __tablename__ = "source_blocks"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="block_ordinal_nonnegative"),
        CheckConstraint(
            "kind IN ('heading','paragraph','list_item','table','metadata','other')",
            name="block_kind_known",
        ),
        UniqueConstraint("document_id", "ordinal", name="uq_source_blocks_document_ordinal"),
        UniqueConstraint("id", "document_id", name="uq_source_blocks_id_document"),
        Index("ix_source_blocks_document", "document_id"),
        Index("ix_source_blocks_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(String(191), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("clean_documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('turkish'::regconfig, immutable_unaccent(COALESCE(text, ''::text)))",
            persisted=True,
        ),
        nullable=False,
    )


class ExtractionCandidateRow(Base):
    """Immutable schema-constrained extraction candidate."""

    __tablename__ = "extraction_candidates"
    __table_args__ = (
        CheckConstraint(
            "method IN ('rule','llm','hybrid','manual')", name="extraction_method_known"
        ),
        CheckConstraint("completed_at >= started_at", name="extraction_times_ordered"),
        UniqueConstraint("candidate_sha256", name="uq_extraction_candidates_candidate_sha256"),
        Index("ix_extraction_candidates_document", "source_document_id"),
    )

    id: Mapped[str] = mapped_column(String(191), primary_key=True)
    source_document_id: Mapped[str] = mapped_column(
        ForeignKey("clean_documents.id"), nullable=False
    )
    bank_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(191), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(191), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(191))
    model_id: Mapped[str | None] = mapped_column(String(191))
    model_digest: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issues: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    candidate_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvidenceRefRow(Base):
    """Canonical field-level evidence, shared by candidates and validated records."""

    __tablename__ = "evidence_refs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["block_id", "source_document_id"],
            ["source_blocks.id", "source_blocks.document_id"],
            name="fk_evidence_block_document",
        ),
        CheckConstraint("start_char >= 0", name="evidence_start_nonnegative"),
        CheckConstraint("end_char > start_char", name="evidence_span_ordered"),
        CheckConstraint(
            "end_char - start_char = char_length(quote)", name="evidence_span_matches_quote"
        ),
        CheckConstraint(
            "status IN ('stated','inferred','unknown','ambiguous')",
            name="evidence_status_known",
        ),
        UniqueConstraint("ref_sha256", name="uq_evidence_refs_ref_sha256"),
        Index("ix_evidence_refs_document_block", "source_document_id", "block_id"),
    )

    id: Mapped[str] = mapped_column(String(191), primary_key=True)
    field_pointer: Mapped[str] = mapped_column(String(500), nullable=False)
    source_document_id: Mapped[str] = mapped_column(String(191), nullable=False)
    block_id: Mapped[str] = mapped_column(String(191), nullable=False)
    quote: Mapped[str] = mapped_column(String(1_000), nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    ref_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class ExtractionEvidenceLink(Base):
    __tablename__ = "extraction_evidence"

    extraction_id: Mapped[str] = mapped_column(
        ForeignKey("extraction_candidates.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence_refs.id"), primary_key=True)


class CampaignRecordRow(Base):
    """One immutable version of a logical campaign record."""

    __tablename__ = "campaign_records"
    __table_args__ = (
        CheckConstraint("version > 0", name="record_version_positive"),
        CheckConstraint(
            "status IN ('validated','needs_review','rejected')", name="record_status_known"
        ),
        CheckConstraint("term_min IS NULL OR term_min >= 0", name="term_min_nonnegative"),
        CheckConstraint("term_max IS NULL OR term_max >= 0", name="term_max_nonnegative"),
        CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_to >= valid_from",
            name="record_validity_ordered",
        ),
        UniqueConstraint("campaign_key", "version", name="uq_campaign_records_key_version"),
        Index("ix_campaign_records_key_record_hash", "campaign_key", "record_sha256"),
        Index("ix_campaign_records_key_payload_hash", "campaign_key", "payload_sha256"),
        Index("ix_campaign_records_bank_observed", "bank_id", "observed_at"),
        Index("ix_campaign_records_family_type", "product_family", "campaign_type"),
        Index("ix_campaign_records_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_campaign_records_data_jsonb",
            "data",
            postgresql_using="gin",
            postgresql_ops={"data": "jsonb_path_ops"},
        ),
    )

    id: Mapped[str] = mapped_column(String(191), primary_key=True)
    campaign_key: Mapped[str] = mapped_column(String(191), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_document_id: Mapped[str] = mapped_column(
        ForeignKey("clean_documents.id"), nullable=False
    )
    bank_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(500))
    product_family: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_type: Mapped[str] = mapped_column(String(64), nullable=False)
    product_currency: Mapped[str | None] = mapped_column(String(3))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    rate_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    rate_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    amount_min: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    amount_max: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    term_min: Mapped[int | None] = mapped_column(Integer)
    term_max: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    extraction: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_issues: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('turkish'::regconfig, "
            "immutable_unaccent((((COALESCE(title, ''::character varying))::text || "
            "' '::text) || (COALESCE(summary, ''::character varying))::text)))",
            persisted=True,
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


Index(
    "ix_campaign_records_title_unaccent_trgm",
    func.immutable_unaccent(CampaignRecordRow.title).label("title_unaccented"),
    postgresql_using="gin",
    postgresql_ops={"title_unaccented": "gin_trgm_ops"},
)
Index(
    "ix_campaign_records_logical_latest",
    CampaignRecordRow.campaign_key,
    CampaignRecordRow.observed_at.desc(),
    CampaignRecordRow.version.desc(),
    CampaignRecordRow.id.asc(),
)


class CampaignObservationRow(Base):
    """One immutable scan observation mapped to its canonical campaign version."""

    __tablename__ = "campaign_observations"
    __table_args__ = (
        UniqueConstraint(
            "campaign_key",
            "observation_key",
            name="uq_campaign_observations_key_observation",
        ),
        Index(
            "ix_campaign_observations_campaign_observed",
            "campaign_key",
            "observed_at",
        ),
        Index("ix_campaign_observations_record_id", "record_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    campaign_key: Mapped[str] = mapped_column(String(191), nullable=False)
    observation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    scan_run_id: Mapped[str] = mapped_column(String(191), nullable=False)
    record_id: Mapped[str] = mapped_column(ForeignKey("campaign_records.id"), nullable=False)
    clean_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RecordEvidenceLink(Base):
    __tablename__ = "record_evidence"

    record_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_records.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence_refs.id"), primary_key=True)


class CoverageEntryRow(Base):
    """Append-only observation of source and campaign coverage."""

    __tablename__ = "coverage_entries"
    __table_args__ = (
        CheckConstraint("source_count >= 0", name="coverage_source_count_nonnegative"),
        CheckConstraint("campaign_count >= 0", name="coverage_campaign_count_nonnegative"),
        CheckConstraint(
            "status IN ('success','not_found','unreachable','blocked','partial')",
            name="coverage_status_known",
        ),
        UniqueConstraint("entry_sha256", name="uq_coverage_entries_entry_sha256"),
        Index("ix_coverage_entries_source_observed", "source_id", "observed_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(500), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    campaign_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    entry_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DurableJob(Base):
    """Database-backed unit of work with a fencing lease token."""

    __tablename__ = "durable_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','dead')", name="job_status_known"
        ),
        CheckConstraint("attempts >= 0", name="job_attempts_nonnegative"),
        CheckConstraint("max_attempts > 0", name="job_max_attempts_positive"),
        CheckConstraint("attempts <= max_attempts", name="job_attempts_bounded"),
        UniqueConstraint("kind", "dedupe_key", name="uq_durable_jobs_kind_dedupe"),
        Index(
            "ix_durable_jobs_claim",
            "priority",
            "available_at",
            postgresql_where=text("status IN ('queued','running')"),
        ),
        Index("ix_durable_jobs_lease_expiry", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(191))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    leased_by: Mapped[str | None] = mapped_column(String(191))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Base):
    """Transactional outbox event with the same fencing semantics as jobs."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="outbox_attempts_nonnegative"),
        CheckConstraint("max_attempts > 0", name="outbox_max_attempts_positive"),
        UniqueConstraint("dedupe_key", name="uq_outbox_events_dedupe_key"),
        UniqueConstraint("feed_sequence", name="uq_outbox_events_feed_sequence"),
        Index(
            "ix_outbox_events_claim",
            "available_at",
            postgresql_where=text("published_at IS NULL"),
        ),
        Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
        Index(
            "ix_outbox_events_notification_feed",
            "topic",
            "event_type",
            "feed_sequence",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    feed_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    topic: Mapped[str] = mapped_column(String(191), nullable=False)
    event_type: Mapped[str] = mapped_column(String(191), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(191), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(191), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    leased_by: Mapped[str | None] = mapped_column(String(191))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ComparisonSnapshot(Base):
    """Content-addressed comparison request and explainable response snapshot."""

    __tablename__ = "comparison_snapshots"
    __table_args__ = (
        UniqueConstraint("request_sha256", name="uq_comparison_snapshots_request_sha256"),
        Index("ix_comparison_snapshots_canonical_sha256", "canonical_sha256"),
        Index("ix_comparison_snapshots_generated", "generated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ruleset_version: Mapped[str] = mapped_column(String(191), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ComparisonRecordLink(Base):
    __tablename__ = "comparison_record_links"

    comparison_id: Mapped[UUID] = mapped_column(
        ForeignKey("comparison_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    record_id: Mapped[str] = mapped_column(ForeignKey("campaign_records.id"), primary_key=True)


class AuthUser(Base):
    """Local administrator identity with only an Argon2id PHC credential."""

    __tablename__ = "auth_users"
    __table_args__ = (
        CheckConstraint("username ~ '^[a-z][a-z0-9_-]{0,63}$'", name="username_canonical"),
        CheckConstraint("password_hash LIKE '$argon2id$v=19$%'", name="password_hash_argon2id_v19"),
        CheckConstraint("jsonb_typeof(roles) = 'array'", name="roles_array"),
        CheckConstraint("failed_attempts >= 0", name="failed_attempts_nonnegative"),
        Index("uq_auth_users_username_lower", func.lower(text("username")), unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_authenticated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuthSession(Base):
    """Hashed bearer-session material; raw tokens are never persisted."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="session_token_hash_sha256"),
        CheckConstraint("csrf_hash ~ '^[0-9a-f]{64}$'", name="session_csrf_hash_sha256"),
        CheckConstraint("token_hash <> csrf_hash", name="session_hashes_distinct"),
        CheckConstraint(
            "absolute_expires_at > created_at", name="session_absolute_expiry_after_creation"
        ),
        CheckConstraint("last_seen_at >= created_at", name="session_last_seen_after_creation"),
        CheckConstraint("idle_expires_at > last_seen_at", name="session_idle_after_last_seen"),
        CheckConstraint(
            "idle_expires_at <= absolute_expires_at", name="session_idle_within_absolute"
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at", name="session_revoked_after_creation"
        ),
        UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        UniqueConstraint("csrf_hash", name="uq_auth_sessions_csrf_hash"),
        Index(
            "ix_auth_sessions_user_active",
            "user_id",
            "revoked_at",
            "idle_expires_at",
            "absolute_expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def expires_at(self) -> datetime:
        """Compatibility alias for the former undifferentiated expiry field."""

        return self.absolute_expires_at

    @property
    def client_context(self) -> dict[str, Any]:
        """Compatibility view: raw client context is no longer persisted."""

        return {}


class AuthRateReservation(Base):
    """Durable reservation used by transactionally locked login windows."""

    __tablename__ = "auth_rate_reservations"
    __table_args__ = (
        CheckConstraint("dimension IN ('username','client')", name="rate_dimension_known"),
        CheckConstraint("subject_hash ~ '^[0-9a-f]{64}$'", name="rate_subject_hash_sha256"),
        Index(
            "ix_auth_rate_reservations_window",
            "dimension",
            "subject_hash",
            "reserved_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dimension: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthAuditEvent(Base):
    """Redacted security event protected by append-only database triggers."""

    __tablename__ = "auth_audit_events"
    __table_args__ = (
        CheckConstraint("event_type ~ '^[a-z][a-z0-9_.-]{0,63}$'", name="audit_event_type_known"),
        CheckConstraint("outcome IN ('success','failure','denied')", name="audit_outcome_known"),
        CheckConstraint(
            "username_hash IS NULL OR username_hash ~ '^[0-9a-f]{64}$'",
            name="audit_username_hash_sha256",
        ),
        CheckConstraint(
            "client_hash IS NULL OR client_hash ~ '^[0-9a-f]{64}$'",
            name="audit_client_hash_sha256",
        ),
        CheckConstraint(
            "reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_.-]{0,63}$'",
            name="audit_reason_code_known",
        ),
        Index("ix_auth_audit_events_occurred", "occurred_at", "id"),
        Index("ix_auth_audit_events_actor", "actor_user_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("auth_users.id"))
    session_id: Mapped[UUID | None] = mapped_column(ForeignKey("auth_sessions.id"))
    username_hash: Mapped[str | None] = mapped_column(String(64))
    client_hash: Mapped[str | None] = mapped_column(String(64))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(191))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
