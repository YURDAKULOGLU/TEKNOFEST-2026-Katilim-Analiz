"""Compliant collection, fixture import, and evidence-preserving HTML cleaning."""

from katilim_analiz.ingestion.artifacts import (
    ArtifactStoreError,
    FetchResult,
    MemoryArtifactStore,
    PrivateFileArtifactStore,
    RawArtifactStore,
)
from katilim_analiz.ingestion.cache import (
    InMemoryResponseCache,
    ResponseCache,
    ResponseCacheEntry,
)
from katilim_analiz.ingestion.cleaning import CleaningError, clean_html
from katilim_analiz.ingestion.fetcher import HttpIngestor
from katilim_analiz.ingestion.fixtures import FixtureImportError, import_html_fixture
from katilim_analiz.ingestion.policy import (
    HostPolicy,
    InMemoryHostRateLimiter,
    PolicyViolation,
    StaticAddressResolver,
    StaticHostPolicyProvider,
    SystemAddressResolver,
    validate_target,
)
from katilim_analiz.ingestion.registry import (
    BankRegistry,
    BankSource,
    RegistryValidationError,
    load_registry,
)
from katilim_analiz.ingestion.robots import (
    InMemoryRobotsCache,
    RobotsDecision,
    evaluate_robots,
)

__all__ = [
    "ArtifactStoreError",
    "BankRegistry",
    "BankSource",
    "CleaningError",
    "FetchResult",
    "FixtureImportError",
    "HostPolicy",
    "HttpIngestor",
    "InMemoryHostRateLimiter",
    "InMemoryResponseCache",
    "InMemoryRobotsCache",
    "MemoryArtifactStore",
    "PolicyViolation",
    "PrivateFileArtifactStore",
    "RawArtifactStore",
    "RegistryValidationError",
    "ResponseCache",
    "ResponseCacheEntry",
    "RobotsDecision",
    "StaticAddressResolver",
    "StaticHostPolicyProvider",
    "SystemAddressResolver",
    "clean_html",
    "evaluate_robots",
    "import_html_fixture",
    "load_registry",
    "validate_target",
]
