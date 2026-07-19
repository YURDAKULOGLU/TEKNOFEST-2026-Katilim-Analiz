"""PostgreSQL persistence boundary for API, worker, and migration roles."""

from katilim_analiz.storage.database import (
    Database,
    DatabaseConfigurationError,
    create_engine,
    validated_asyncpg_url,
)
from katilim_analiz.storage.health import MigrationHeadError, PostgresDatabaseHealth
from katilim_analiz.storage.read_adapter import (
    InvalidReadCursorError,
    PostgresCampaignReadAdapter,
    ReadModelIntegrityError,
)
from katilim_analiz.storage.repositories import (
    ArtifactRepository,
    AuthRepository,
    CampaignRepository,
    ComparisonRepository,
    CoverageRepository,
    EvidenceIntegrityError,
    EvidenceRepository,
    ExtractionRepository,
    ImmutableConflictError,
    JobLease,
    JobRepository,
    LeaseLostError,
    OutboxLease,
    OutboxRepository,
    PersistResult,
    SourceRepository,
    StaleSourceVersionError,
    StorageError,
    comparison_request_sha256,
)
from katilim_analiz.storage.write_adapter import (
    PostgresCampaignWriteAdapter,
    RegistrySourceMismatchError,
    RegistrySourceMissingError,
)

__all__ = [
    "ArtifactRepository",
    "AuthRepository",
    "CampaignRepository",
    "ComparisonRepository",
    "CoverageRepository",
    "Database",
    "DatabaseConfigurationError",
    "EvidenceIntegrityError",
    "EvidenceRepository",
    "ExtractionRepository",
    "ImmutableConflictError",
    "InvalidReadCursorError",
    "JobLease",
    "JobRepository",
    "LeaseLostError",
    "MigrationHeadError",
    "OutboxLease",
    "OutboxRepository",
    "PersistResult",
    "PostgresCampaignReadAdapter",
    "PostgresCampaignWriteAdapter",
    "PostgresDatabaseHealth",
    "ReadModelIntegrityError",
    "RegistrySourceMismatchError",
    "RegistrySourceMissingError",
    "SourceRepository",
    "StaleSourceVersionError",
    "StorageError",
    "comparison_request_sha256",
    "create_engine",
    "validated_asyncpg_url",
]
