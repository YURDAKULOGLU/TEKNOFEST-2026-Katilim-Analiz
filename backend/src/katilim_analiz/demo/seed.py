"""Replay a bounded published evidence snapshot into PostgreSQL without upgrading review state."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

from katilim_analiz.application.processing import (
    CoverageObservation,
    PersistenceBundle,
    SourceRequest,
)
from katilim_analiz.contracts import (
    CampaignRecord,
    CampaignType,
    CleanDocument,
    CoverageEntry,
    CoverageStatus,
    EvidenceStatus,
    FetchArtifact,
    FetchStatus,
    ProductFamily,
    RateValue,
    RecordStatus,
    SourceBlock,
    ValidityWindow,
)
from katilim_analiz.extraction.candidate import build_candidate
from katilim_analiz.extraction.draft import BoundFact, ExtractionDraft
from katilim_analiz.extraction.evidence import TextSpan
from katilim_analiz.ingestion.registry import BankRegistry
from katilim_analiz.runtime.paths import resolve_runtime_path
from katilim_analiz.runtime.registry import load_runtime_registry, sync_source_registry
from katilim_analiz.storage.database import Database
from katilim_analiz.storage.repositories import CoverageRepository
from katilim_analiz.storage.serialization import canonical_sha256
from katilim_analiz.storage.write_adapter import PostgresCampaignWriteAdapter

DEFAULT_DEMO_SEED_PATH = Path("datasets/demo/v1/seed.json")
MAX_DEMO_SEED_BYTES = 1_048_576
DEMO_EXTRACTOR_VERSION = "demo-evidence-replay/1.0.0"

Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9][a-z0-9-]{0,99}$"),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
JsonPointer = Annotated[str, StringConstraints(pattern=r"^(?:/(?:[^~/]|~[01])*)*$")]
Issue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class _SeedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DemoSourceAsset(_SeedModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def path_is_repository_relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not value.startswith("datasets/"):
            raise ValueError("source asset path must stay under datasets/")
        return value


class DemoCoverage(_SeedModel):
    """Collector observation kept distinct from validated public record counts."""

    bank_id: Identifier
    bank_name: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    observed_at: datetime
    status: CoverageStatus
    source_count: Annotated[int, Field(ge=0)]
    source_candidate_count: Annotated[int, Field(ge=0)]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=500)]

    @field_validator("observed_at")
    @classmethod
    def observed_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone offset")
        return value


class DemoEvidence(_SeedModel):
    field_pointer: JsonPointer
    quote: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    evidence_sha256: Sha256
    source_block_id: Annotated[str, StringConstraints(min_length=1, max_length=191)]
    source_start_char: Annotated[int, Field(ge=0)]
    source_end_char: Annotated[int, Field(gt=0)]
    status: EvidenceStatus

    @model_validator(mode="after")
    def source_span_and_hash_match_quote(self) -> DemoEvidence:
        if self.source_end_char - self.source_start_char != len(self.quote):
            raise ValueError("source evidence offsets must match quote length")
        if hashlib.sha256(self.quote.encode()).hexdigest() != self.evidence_sha256:
            raise ValueError("source evidence SHA-256 does not match quote")
        if self.status not in {EvidenceStatus.STATED, EvidenceStatus.INFERRED}:
            raise ValueError("demo evidence must be stated or inferred")
        return self


class DemoCampaign(_SeedModel):
    seed_id: Identifier
    source_example_id: Annotated[str, StringConstraints(min_length=1, max_length=191)]
    bank_id: Identifier
    source_url: HttpUrl
    observed_at: datetime
    source_raw_sha256: Sha256
    source_clean_sha256: Sha256
    human_review_status: Literal["pending"]
    title: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    product_family: ProductFamily = ProductFamily.UNKNOWN
    campaign_type: CampaignType = CampaignType.UNKNOWN
    rates: Annotated[list[RateValue], Field(max_length=20)] = Field(default_factory=list)
    validity: ValidityWindow | None = None
    evidence: Annotated[list[DemoEvidence], Field(min_length=1, max_length=100)]
    issues: Annotated[list[Issue], Field(min_length=1, max_length=100)]

    @field_validator("observed_at")
    @classmethod
    def observed_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone offset")
        return value

    @model_validator(mode="after")
    def displayed_fields_have_published_evidence(self) -> DemoCampaign:
        by_pointer: dict[str, list[DemoEvidence]] = {}
        for item in self.evidence:
            by_pointer.setdefault(item.field_pointer, []).append(item)

        if not any(item.quote == self.title for item in by_pointer.get("/data/title", [])):
            raise ValueError("demo title must equal a published evidence quote")
        if self.product_family is not ProductFamily.UNKNOWN and not by_pointer.get(
            "/data/product_family"
        ):
            raise ValueError("demo product family needs published evidence")
        if self.campaign_type is not CampaignType.UNKNOWN and not by_pointer.get(
            "/data/campaign_type"
        ):
            raise ValueError("demo campaign type needs published evidence")
        for index, rate in enumerate(self.rates):
            if not any(
                item.quote == rate.raw for item in by_pointer.get(f"/data/rates/{index}", [])
            ):
                raise ValueError(f"demo rate {index} must equal a published evidence quote")
        if self.validity is not None and not any(
            item.quote == self.validity.raw for item in by_pointer.get("/data/validity", [])
        ):
            raise ValueError("demo validity must equal a published evidence quote")
        if "human_review_pending" not in self.issues:
            raise ValueError("demo campaign must retain the pending human-review issue")
        return self


class DemoSeed(_SeedModel):
    schema_version: Literal["1.0"]
    dataset_id: Literal["katilim-demo-seed"]
    dataset_version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]
    created_at: datetime
    source_registry_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    source_coverage: DemoSourceAsset
    source_examples: DemoSourceAsset
    coverage: Annotated[list[DemoCoverage], Field(min_length=10, max_length=10)]
    campaigns: Annotated[list[DemoCampaign], Field(min_length=1, max_length=20)]
    limitations: Annotated[list[Issue], Field(min_length=1, max_length=20)]

    @field_validator("created_at")
    @classmethod
    def created_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone offset")
        return value

    @model_validator(mode="after")
    def identities_are_unique_and_covered(self) -> DemoSeed:
        coverage_ids = [item.bank_id for item in self.coverage]
        if len(set(coverage_ids)) != 10:
            raise ValueError("demo coverage must contain ten unique bank IDs")
        campaign_ids = [item.seed_id for item in self.campaigns]
        if len(campaign_ids) != len(set(campaign_ids)):
            raise ValueError("demo campaign seed IDs must be unique")
        if any(item.bank_id not in coverage_ids for item in self.campaigns):
            raise ValueError("every demo campaign bank must exist in coverage")
        return self


class DemoSeedResult(_SeedModel):
    dataset_version: str
    registry_version: str
    coverage_count: int
    coverage_created: int
    campaign_count: int
    campaigns_created: int
    human_verified_count: Literal[0] = 0
    status: Literal["seeded"] = "seeded"


def load_demo_seed(path: str | Path | None = None) -> DemoSeed:
    """Load one bounded strict JSON snapshot; unknown fields fail closed."""

    resolved = resolve_runtime_path(path or DEFAULT_DEMO_SEED_PATH)
    size = resolved.stat().st_size
    if size > MAX_DEMO_SEED_BYTES:
        raise ValueError(f"demo seed exceeds the {MAX_DEMO_SEED_BYTES}-byte parsing limit: {size}")
    return DemoSeed.model_validate_json(resolved.read_bytes())


def _source_blocks(
    seed: DemoSeed, campaign: DemoCampaign
) -> tuple[list[SourceBlock], dict[int, SourceBlock]]:
    blocks: list[SourceBlock] = []
    block_by_source_span: dict[tuple[str, int, int, str], SourceBlock] = {}
    evidence_blocks: dict[int, SourceBlock] = {}
    for index, item in enumerate(campaign.evidence):
        source_span = (
            item.source_block_id,
            item.source_start_char,
            item.source_end_char,
            item.quote,
        )
        block = block_by_source_span.get(source_span)
        if block is None:
            locator = (
                f"{seed.source_examples.path}#example={campaign.source_example_id};"
                f"block={item.source_block_id};"
                f"chars={item.source_start_char}:{item.source_end_char}"
            )
            digest = canonical_sha256(
                {
                    "dataset_version": seed.dataset_version,
                    "seed_id": campaign.seed_id,
                    "source_span": source_span,
                }
            )
            block = SourceBlock(
                id=f"block:demo:{digest}",
                ordinal=len(blocks),
                kind="other",
                text=item.quote,
                locator=locator,
                text_sha256=item.evidence_sha256,
            )
            blocks.append(block)
            block_by_source_span[source_span] = block
        evidence_blocks[index] = block
    return blocks, evidence_blocks


def _document_and_fetch(
    seed: DemoSeed, campaign: DemoCampaign
) -> tuple[FetchArtifact, CleanDocument, dict[int, SourceBlock]]:
    blocks, evidence_blocks = _source_blocks(seed, campaign)
    replay_bytes = "\n".join(block.text for block in blocks).encode()
    replay_sha256 = hashlib.sha256(replay_bytes).hexdigest()
    fetch_digest = canonical_sha256(
        {
            "dataset_version": seed.dataset_version,
            "seed_id": campaign.seed_id,
            "source_url": str(campaign.source_url),
            "observed_at": campaign.observed_at,
            "replay_sha256": replay_sha256,
        }
    )
    fetched = FetchArtifact(
        id=f"fetch:demo:{fetch_digest}",
        bank_id=campaign.bank_id,
        requested_url=campaign.source_url,
        final_url=campaign.source_url,
        status=FetchStatus.SUCCESS,
        fetched_at=campaign.observed_at,
        robots_allowed=True,
        content_type="application/vnd.katilim-analiz.evidence-replay+text",
        raw_sha256=replay_sha256,
        raw_size_bytes=len(replay_bytes),
        private_raw_path=(
            f"{DEFAULT_DEMO_SEED_PATH.as_posix()}#campaign={campaign.seed_id};evidence"
        ),
    )
    clean_sha256 = canonical_sha256(
        {
            "dataset_version": seed.dataset_version,
            "seed_id": campaign.seed_id,
            "source_clean_sha256": campaign.source_clean_sha256,
            "blocks": [block.model_dump(mode="json") for block in blocks],
        }
    )
    document_digest = canonical_sha256(
        {
            "fetch_artifact_id": fetched.id,
            "cleaner_version": DEMO_EXTRACTOR_VERSION,
            "clean_sha256": clean_sha256,
        }
    )
    document = CleanDocument(
        id=f"clean:demo:{document_digest}",
        fetch_artifact_id=fetched.id,
        bank_id=campaign.bank_id,
        canonical_url=campaign.source_url,
        title=campaign.title,
        cleaned_at=campaign.observed_at,
        cleaner_version=DEMO_EXTRACTOR_VERSION,
        clean_sha256=clean_sha256,
        language="tr",
        blocks=blocks,
    )
    return fetched, document, evidence_blocks


def _draft(
    campaign: DemoCampaign,
    evidence_blocks: dict[int, SourceBlock],
) -> ExtractionDraft:
    by_pointer: dict[str, list[tuple[DemoEvidence, SourceBlock]]] = {}
    for index, item in enumerate(campaign.evidence):
        by_pointer.setdefault(item.field_pointer, []).append((item, evidence_blocks[index]))

    def fact[T](pointer: str, value: T) -> BoundFact[T]:
        item, block = by_pointer[pointer][0]
        return BoundFact(
            value=value,
            span=TextSpan(block.id, item.quote, 0, len(item.quote)),
            inferred=item.status is EvidenceStatus.INFERRED,
        )

    return ExtractionDraft(
        bank_id=campaign.bank_id,
        title=fact("/data/title", campaign.title),
        product_family=(
            None
            if campaign.product_family is ProductFamily.UNKNOWN
            else fact("/data/product_family", campaign.product_family)
        ),
        campaign_type=(
            None
            if campaign.campaign_type is CampaignType.UNKNOWN
            else fact("/data/campaign_type", campaign.campaign_type)
        ),
        rates=tuple(
            fact(f"/data/rates/{index}", rate) for index, rate in enumerate(campaign.rates)
        ),
        validity=(None if campaign.validity is None else fact("/data/validity", campaign.validity)),
        issues=tuple(campaign.issues),
    )


def materialize_campaign(
    seed: DemoSeed,
    campaign: DemoCampaign,
    *,
    bank_name: str,
    record_id: str,
    version: int,
) -> PersistenceBundle:
    """Build the same immutable, validated bundle on every replay."""

    fetched, document, evidence_blocks = _document_and_fetch(seed, campaign)
    candidate = build_candidate(
        _draft(campaign, evidence_blocks),
        document,
        started_at=campaign.observed_at,
        completed_at=campaign.observed_at,
        extractor_version=DEMO_EXTRACTOR_VERSION,
    )
    campaign_key = f"demo:{campaign.seed_id}"
    record_sha256 = canonical_sha256(
        {
            "campaign_key": campaign_key,
            "candidate_id": candidate.id,
            "status": RecordStatus.NEEDS_REVIEW,
            "validation_issues": candidate.issues,
        }
    )
    record = CampaignRecord(
        id=record_id,
        version=version,
        source_document_id=document.id,
        observed_at=campaign.observed_at,
        data=candidate.data,
        evidence=candidate.evidence,
        extraction=candidate.metadata,
        status=RecordStatus.NEEDS_REVIEW,
        validation_issues=candidate.issues,
        record_sha256=record_sha256,
    )
    return PersistenceBundle(
        source=SourceRequest(
            bank_id=campaign.bank_id,
            bank_name=bank_name,
            source_url=campaign.source_url,
            campaign_key=campaign_key,
            scan_run_id=f"demo-seed:{seed.dataset_id}:{seed.dataset_version}",
        ),
        fetch_artifact=fetched,
        clean_document=document,
        candidate=candidate,
        record=record,
        coverage_observation=CoverageObservation(
            bank_id=campaign.bank_id,
            bank_name=bank_name,
            observed_at=campaign.observed_at,
            status=CoverageStatus.PARTIAL,
            reason="demo_seed_pending_human_review",
        ),
    )


def _validate_registry(seed: DemoSeed, registry: BankRegistry) -> None:
    if registry.registry_version != seed.source_registry_version:
        raise ValueError("demo seed registry version differs from the active versioned registry")
    coverage_names = {item.bank_id: item.bank_name for item in seed.coverage}
    registry_names = {item.id: item.legal_name for item in registry.banks}
    if coverage_names != registry_names:
        raise ValueError("demo coverage identities differ from the versioned registry")


async def seed_demo(
    database: Database,
    *,
    seed_path: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> DemoSeedResult:
    """Idempotently seed source registry, pending records, then the ten-bank snapshot."""

    seed = load_demo_seed(seed_path)
    registry = load_runtime_registry(registry_path)
    _validate_registry(seed, registry)
    await sync_source_registry(database, registry)

    writer = PostgresCampaignWriteAdapter(database.session_factory)
    created_campaigns = 0
    for campaign in seed.campaigns:
        bank = registry.bank(campaign.bank_id)
        provisional = materialize_campaign(
            seed,
            campaign,
            bank_name=bank.legal_name,
            record_id="record:demo:provisional",
            version=1,
        )
        persisted = await writer.persist(provisional)
        created_campaigns += int(persisted.record_created is True)

    created_coverage = 0
    async with database.transaction() as session:
        coverage = CoverageRepository(session)
        for entry in seed.coverage:
            public_entry = CoverageEntry(
                bank_id=entry.bank_id,
                bank_name=entry.bank_name,
                observed_at=entry.observed_at,
                status=entry.status,
                source_count=entry.source_count,
                campaign_count=0,
                reason=entry.reason,
            )
            created_coverage += int(await coverage.add(public_entry))

    return DemoSeedResult(
        dataset_version=seed.dataset_version,
        registry_version=registry.registry_version,
        coverage_count=len(seed.coverage),
        coverage_created=created_coverage,
        campaign_count=len(seed.campaigns),
        campaigns_created=created_campaigns,
    )
