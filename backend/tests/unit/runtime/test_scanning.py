from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from katilim_analiz.application.processing import campaign_observation_key
from katilim_analiz.cli import main, scan_identity_from_environment
from katilim_analiz.contracts import FetchArtifact, FetchStatus
from katilim_analiz.ingestion.artifacts import FetchResult, create_fetch_artifact
from katilim_analiz.ingestion.discovery import DiscoveryMode
from katilim_analiz.ingestion.registry import BankRegistry, BankSource, RegistryValidationError
from katilim_analiz.ingestion.robots import RobotsDecision
from katilim_analiz.runtime.registry import (
    CampaignSourceStatus,
    MonitoredCampaignSource,
    MonitoredCampaignSourceRegistry,
    load_monitored_campaign_registry,
    load_runtime_registry,
)
from katilim_analiz.runtime.scanning import (
    CampaignScanner,
    CampaignTarget,
    DetailScanJob,
    SourceIndexObservation,
    campaign_key_for_url,
    scan_job_dedupe_key,
)
from katilim_analiz.storage.repositories import monitored_campaign_key

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[4]
CAMPAIGN_REGISTRY = PROJECT_ROOT / "data/registry/monitored-campaign-sources-2026-07-19.json"
CAMPAIGN_SCHEMA = PROJECT_ROOT / "data/registry/monitored-campaign-sources.schema.json"
CRONJOB = PROJECT_ROOT / "deploy/k8s/operations/campaign-scan-cronjob.yaml"


def _bank_registry() -> BankRegistry:
    return BankRegistry(
        schema_version="1.0",
        registry_id="bddk-participation-banks",
        registry_version="2026-07-18.2",
        source_observed_on=date(2026, 7, 18),
        source_url="https://www.bddk.org.tr/Kurulus/Liste/77",
        banks=(
            BankSource(
                id="bank-a",
                listing_order=1,
                legal_name="Banka A",
                listed_homepage_url="https://bank.example",
                allowed_hosts=("bank.example",),
                digital_bank=False,
            ),
        ),
    )


def _campaign_registry(
    *, mode: DiscoveryMode = DiscoveryMode.DETAIL_LINKS
) -> MonitoredCampaignSourceRegistry:
    source = MonitoredCampaignSource(
        bank_id="bank-a",
        status=CampaignSourceStatus.VERIFIED,
        observed_on=date(2026, 7, 19),
        evidence_url="https://bank.example/kampanyalar/",
        discovery_mode=mode,
        index_url="https://bank.example/kampanyalar/",
        detail_path_prefixes=("/kampanyalar/",) if mode is DiscoveryMode.DETAIL_LINKS else (),
        max_links=20 if mode is DiscoveryMode.DETAIL_LINKS else 0,
    )
    return MonitoredCampaignSourceRegistry(
        schema_version="1.0",
        registry_id="monitored-campaign-sources",
        registry_version="2026-07-19.1",
        source_observed_on=date(2026, 7, 19),
        bank_registry_version="2026-07-18.2",
        sources=(source,),
    )


def _fetch(html: bytes, *, status: FetchStatus = FetchStatus.SUCCESS) -> FetchResult:
    digest = hashlib.sha256(html).hexdigest()
    artifact = create_fetch_artifact(
        bank_id="bank-a",
        requested_url="https://bank.example/kampanyalar/",
        final_url="https://bank.example/kampanyalar/",
        status=status,
        http_status=200 if status is FetchStatus.SUCCESS else 503,
        fetched_at=NOW,
        robots_allowed=True,
        content_type="text/html",
        raw_sha256=digest if status is FetchStatus.SUCCESS else None,
        raw_size_bytes=len(html) if status is FetchStatus.SUCCESS else 0,
        error_code=None if status is FetchStatus.SUCCESS else "http_503",
        error_detail=None if status is FetchStatus.SUCCESS else "server unavailable",
    )
    return FetchResult(
        artifact=artifact,
        raw_content=html if status is FetchStatus.SUCCESS else None,
        robots_decision=RobotsDecision(True, "allowed"),
    )


class FakeCollector:
    def __init__(self, result: FetchResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def fetch(self, bank_id: str, url: str) -> FetchResult:
        self.calls.append((bank_id, url))
        return self.result


class FakeScanStore:
    def __init__(self) -> None:
        self.targets: dict[str, dict[str, CampaignTarget]] = {}
        self.index_hashes: dict[tuple[str, str], str] = {}
        self.job_dedupe: set[str] = set()
        self.jobs: list[DetailScanJob] = []
        self.observed_artifact_ids: list[str] = []
        self.deactivation_calls = 0

    async def list_active(self, bank_id: str) -> tuple[CampaignTarget, ...]:
        return tuple(sorted(self.targets.get(bank_id, {}).values(), key=lambda item: item.url))

    async def observe_index(
        self,
        artifact: FetchArtifact,
        *,
        registry_version: str,
    ) -> SourceIndexObservation:
        del registry_version
        self.observed_artifact_ids.append(artifact.id)
        key = (artifact.bank_id, str(artifact.requested_url))
        previous = self.index_hashes.get(key)
        current = previous
        changed = False
        if artifact.status is FetchStatus.SUCCESS:
            current = artifact.raw_sha256
            assert current is not None
            changed = previous is not None and previous != current
            self.index_hashes[key] = current
        return SourceIndexObservation(previous, current, changed)

    async def observe_index_error(
        self,
        *,
        bank_id: str,
        index_url: str,
        registry_version: str,
        observed_at: datetime,
        error_code: str,
    ) -> None:
        del bank_id, index_url, registry_version, observed_at, error_code

    async def upsert_seen(
        self,
        *,
        bank_id: str,
        campaign_key: str,
        canonical_url: str,
        discovered_from: str,
        registry_version: str,
        observed_at: datetime,
    ) -> tuple[CampaignTarget, bool]:
        del discovered_from, registry_version, observed_at
        bank_targets = self.targets.setdefault(bank_id, {})
        created = canonical_url not in bank_targets
        target = CampaignTarget(bank_id, campaign_key, canonical_url)
        bank_targets[canonical_url] = target
        return target, created

    async def enqueue_detail(self, job: DetailScanJob, *, dedupe_key: str) -> bool:
        if dedupe_key in self.job_dedupe:
            return False
        self.job_dedupe.add(dedupe_key)
        self.jobs.append(job)
        return True


def test_registry_schema_bank_set_status_rules_and_official_hosts(tmp_path: Path) -> None:
    payload = json.loads(CAMPAIGN_REGISTRY.read_text(encoding="utf-8"))
    schema = json.loads(CAMPAIGN_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(payload)

    bank_registry = load_runtime_registry()
    registry = load_monitored_campaign_registry(bank_registry=bank_registry)
    assert tuple(source.bank_id for source in registry.sources) == tuple(
        bank.id for bank in bank_registry.banks
    )
    assert {source.bank_id for source in registry.verified_sources} == {
        "albaraka-turk",
        "dunya-katilim",
        "hayat-finans",
        "kuveyt-turk",
        "tom-katilim",
        "emlak-katilim",
        "vakif-katilim",
        "ziraat-katilim",
    }
    assert {source.bank_id for source in registry.unavailable_sources} == {
        "adil-katilim",
        "turkiye-finans",
    }

    guessed = copy.deepcopy(payload)
    guessed["sources"][0]["index_url"] = "https://www.adilkatilim.com.tr/kampanyalar"
    assert list(validator.iter_errors(guessed))

    unsafe_prefix = copy.deepcopy(payload)
    unsafe_prefix["sources"][1]["detail_path_prefixes"] = ["/../"]
    assert list(validator.iter_errors(unsafe_prefix))

    wrong_host = copy.deepcopy(payload)
    wrong_host["sources"][1]["index_url"] = "https://attacker.example/kampanyalar"
    path = tmp_path / "wrong-host.json"
    path.write_text(json.dumps(wrong_host), encoding="utf-8")
    with pytest.raises((RegistryValidationError, ValueError), match=r"allowlisted|approved"):
        load_monitored_campaign_registry(path, bank_registry=bank_registry)

    wrong_set = copy.deepcopy(payload)
    wrong_set["sources"][1] = copy.deepcopy(wrong_set["sources"][0])
    path = tmp_path / "wrong-set.json"
    path.write_text(json.dumps(wrong_set), encoding="utf-8")
    with pytest.raises(RegistryValidationError, match="IDs and order"):
        load_monitored_campaign_registry(path, bank_registry=bank_registry)


def test_scan_and_observation_identities_match_wp111_persistence_contract() -> None:
    url = "https://bank.example/kampanyalar/one"
    campaign_key = campaign_key_for_url("bank-a", url)

    assert campaign_key == monitored_campaign_key("bank-a", url)
    assert scan_job_dedupe_key("scan-001", campaign_key, url) == campaign_observation_key(
        "scan-001",
        campaign_key,
        url,
    )


@pytest.mark.asyncio
async def test_same_scan_retry_dedupes_but_new_scan_reenqueues() -> None:
    html = b"""
      <a href="/kampanyalar/b">B</a>
      <a href="/kampanyalar/a">A</a>
    """
    collector = FakeCollector(_fetch(html))
    store = FakeScanStore()
    scanner = CampaignScanner(
        bank_registry=_bank_registry(),
        campaign_registry=_campaign_registry(),
        collector=collector,
        store=store,
    )

    first = await scanner.run("scan-001")
    retry = await scanner.run("scan-001")
    later = await scanner.run("scan-002")

    assert first.enqueued_jobs == 2
    assert retry.enqueued_jobs == 0
    assert retry.deduplicated_jobs == 2
    assert later.enqueued_jobs == 2
    assert len(store.jobs) == 4
    assert [job.url for job in store.jobs[:2]] == [
        "https://bank.example/kampanyalar/a",
        "https://bank.example/kampanyalar/b",
    ]
    assert {job.scan_run_id for job in store.jobs} == {"scan-001", "scan-002"}


@pytest.mark.asyncio
async def test_index_failure_retains_and_enqueues_known_targets() -> None:
    store = FakeScanStore()
    known = CampaignTarget(
        "bank-a",
        "bank-a:known",
        "https://bank.example/kampanyalar/known",
    )
    store.targets["bank-a"] = {known.url: known}
    scanner = CampaignScanner(
        bank_registry=_bank_registry(),
        campaign_registry=_campaign_registry(),
        collector=FakeCollector(_fetch(b"failure", status=FetchStatus.FAILED)),
        store=store,
    )

    result = await scanner.run("scan-failure")

    assert result.has_failures is True
    assert result.enqueued_jobs == 1
    assert store.targets["bank-a"] == {known.url: known}
    assert store.deactivation_calls == 0
    assert store.jobs[0].url == known.url


@pytest.mark.asyncio
async def test_changed_unsegmented_collection_is_review_only_not_campaign_notification() -> None:
    html = b"<main><h2>Campaign A</h2><h2>Campaign B</h2></main>"
    store = FakeScanStore()
    store.index_hashes[("bank-a", "https://bank.example/kampanyalar/")] = "0" * 64
    scanner = CampaignScanner(
        bank_registry=_bank_registry(),
        campaign_registry=_campaign_registry(mode=DiscoveryMode.UNSEGMENTED_COLLECTION),
        collector=FakeCollector(_fetch(html)),
        store=store,
    )

    result = await scanner.run("scan-collection")

    source = result.sources[0]
    assert source.source_index_changed is True
    assert source.review_required is True
    assert source.discovered_targets == 0
    assert result.enqueued_jobs == 0
    assert store.jobs == []


def test_cronjob_is_idempotent_hardened_and_uses_job_uid_downward_api() -> None:
    manifest = CRONJOB.read_text(encoding="utf-8")
    assert "apiVersion: batch/v1" in manifest
    assert "kind: CronJob" in manifest
    assert "concurrencyPolicy: Forbid" in manifest
    assert "startingDeadlineSeconds:" in manifest
    assert "successfulJobsHistoryLimit:" in manifest
    assert "failedJobsHistoryLimit:" in manifest
    assert "backoffLimit:" in manifest
    assert "restartPolicy: Never" in manifest
    assert "automountServiceAccountToken: false" in manifest
    assert "allowPrivilegeEscalation: false" in manifest
    assert "readOnlyRootFilesystem: true" in manifest
    assert 'drop: ["ALL"]' in manifest
    assert "name: SCAN_RUN_ID" in manifest
    assert "metadata.labels['batch.kubernetes.io/controller-uid']" in manifest
    assert "name: SCAN_JOB_NAME" in manifest
    assert "metadata.labels['batch.kubernetes.io/job-name']" in manifest
    assert "serviceAccountToken:" not in manifest
    assert "kubectl" not in manifest


def test_scan_identity_fails_closed_without_exact_downward_api_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="SCAN_RUN_ID"):
        scan_identity_from_environment({})
    with pytest.raises(ValueError, match="Kubernetes label"):
        scan_identity_from_environment({"SCAN_RUN_ID": "job/uid"})

    monkeypatch.delenv("SCAN_RUN_ID", raising=False)
    with pytest.raises(SystemExit) as missing:
        main(["scan"])
    assert missing.value.code == 2

    assert scan_identity_from_environment(
        {"SCAN_RUN_ID": "controller-uid-1", "SCAN_JOB_NAME": "campaign-scan-123"}
    ) == ("controller-uid-1", "campaign-scan-123")


def test_cronjob_renders_with_kubectl_client_dry_run() -> None:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("kubectl is not installed")
    completed = subprocess.run(  # noqa: S603
        [
            kubectl,
            "create",
            "--dry-run=client",
            "--validate=false",
            "-f",
            str(CRONJOB),
            "-o",
            "name",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "cronjob.batch/campaign-scan" in completed.stdout
