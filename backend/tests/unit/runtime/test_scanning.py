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
    MonitoredStaticPage,
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
CAMPAIGN_REGISTRY = PROJECT_ROOT / "data/registry/monitored-campaign-sources-2026-07-21.json"
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


STATIC_PAGE_URLS = (
    "https://bank.example/finansmanlar/konut",
    "https://bank.example/finansmanlar/tasit",
    "https://bank.example/finansmanlar/ihtiyac",
)


def _static_campaign_registry() -> MonitoredCampaignSourceRegistry:
    source = MonitoredCampaignSource(
        bank_id="bank-a",
        status=CampaignSourceStatus.VERIFIED,
        observed_on=date(2026, 7, 21),
        evidence_url=STATIC_PAGE_URLS[0],
        discovery_mode=DiscoveryMode.STATIC_PAGES,
        static_pages=tuple(
            MonitoredStaticPage(url=url, label=label)
            for url, label in zip(STATIC_PAGE_URLS, ("konut", "tasit", "ihtiyac"), strict=True)
        ),
    )
    return MonitoredCampaignSourceRegistry(
        schema_version="1.0",
        registry_id="monitored-campaign-sources",
        registry_version="2026-07-21.1",
        source_observed_on=date(2026, 7, 21),
        bank_registry_version="2026-07-18.2",
        sources=(source,),
    )


def _fetch(
    html: bytes,
    *,
    status: FetchStatus = FetchStatus.SUCCESS,
    url: str = "https://bank.example/kampanyalar/",
) -> FetchResult:
    digest = hashlib.sha256(html).hexdigest()
    artifact = create_fetch_artifact(
        bank_id="bank-a",
        requested_url=url,
        final_url=url,
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


class UrlKeyedCollector:
    def __init__(self, results: dict[str, FetchResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, str]] = []

    async def fetch(self, bank_id: str, url: str) -> FetchResult:
        self.calls.append((bank_id, url))
        return self.results[url]


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
        del registry_version, observed_at
        bank_targets = self.targets.setdefault(bank_id, {})
        created = canonical_url not in bank_targets
        target = CampaignTarget(bank_id, campaign_key, canonical_url, discovered_from)
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
    grouped_ids: list[str] = []
    for source in registry.sources:
        if not grouped_ids or grouped_ids[-1] != source.bank_id:
            grouped_ids.append(source.bank_id)
    assert tuple(grouped_ids) == tuple(bank.id for bank in bank_registry.banks)
    assert {source.bank_id for source in registry.verified_sources} == {
        "albaraka-turk",
        "dunya-katilim",
        "hayat-finans",
        "kuveyt-turk",
        "tom-katilim",
        "emlak-katilim",
        "turkiye-finans",
        "vakif-katilim",
        "ziraat-katilim",
    }
    assert {source.bank_id for source in registry.unavailable_sources} == {"adil-katilim"}
    static_sources = {
        source.bank_id: source
        for source in registry.verified_sources
        if source.discovery_mode is DiscoveryMode.STATIC_PAGES
    }
    assert set(static_sources) == {
        "albaraka-turk",
        "kuveyt-turk",
        "turkiye-finans",
        "vakif-katilim",
        "ziraat-katilim",
        "emlak-katilim",
    }
    for source in static_sources.values():
        assert [page.label for page in source.static_pages] == ["konut", "tasit", "ihtiyac"]

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
    with pytest.raises(RegistryValidationError, match="unavailable bank"):
        load_monitored_campaign_registry(path, bank_registry=bank_registry)

    ungrouped = copy.deepcopy(payload)
    ungrouped["sources"].append(ungrouped["sources"].pop(2))
    path = tmp_path / "ungrouped.json"
    path.write_text(json.dumps(ungrouped), encoding="utf-8")
    with pytest.raises(RegistryValidationError, match="IDs and order"):
        load_monitored_campaign_registry(path, bank_registry=bank_registry)

    doubled_static = copy.deepcopy(payload)
    doubled_static["sources"].insert(3, copy.deepcopy(doubled_static["sources"][2]))
    path = tmp_path / "doubled-static.json"
    path.write_text(json.dumps(doubled_static), encoding="utf-8")
    with pytest.raises(RegistryValidationError, match="more than one static-page"):
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
        "https://bank.example/kampanyalar/",
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
async def test_captcha_walled_source_is_reported_without_failing_the_run() -> None:
    """A bank that refuses robots must not turn every scheduled scan into a failure."""

    store = FakeScanStore()
    scanner = CampaignScanner(
        bank_registry=_bank_registry(),
        campaign_registry=_campaign_registry(),
        collector=FakeCollector(_fetch(b"challenge", status=FetchStatus.BLOCKED)),
        store=store,
    )

    result = await scanner.run("scan-blocked")

    assert result.has_blocked_sources is True
    assert result.has_failures is False


@pytest.mark.asyncio
async def test_static_pages_source_enqueues_each_listed_url_without_discovery() -> None:
    collector = UrlKeyedCollector(
        {url: _fetch(f"<h1>{url}</h1>".encode(), url=url) for url in STATIC_PAGE_URLS}
    )
    store = FakeScanStore()
    scanner = CampaignScanner(
        bank_registry=_bank_registry(),
        campaign_registry=_static_campaign_registry(),
        collector=collector,
        store=store,
    )

    first = await scanner.run("scan-static-001")
    retry = await scanner.run("scan-static-001")

    assert [url for _, url in collector.calls[:3]] == list(STATIC_PAGE_URLS)
    source = first.sources[0]
    assert source.status.value == "success"
    assert source.discovered_targets == 3
    assert first.enqueued_jobs == 3
    assert retry.enqueued_jobs == 0
    assert retry.deduplicated_jobs == 3
    assert sorted(job.url for job in store.jobs) == sorted(STATIC_PAGE_URLS)
    # Issue #33: the curated registry label travels with each static-page job
    # so extraction can use it as a source-metadata hint.
    assert {job.url: job.static_page_label for job in store.jobs} == dict(
        zip(STATIC_PAGE_URLS, ("konut", "tasit", "ihtiyac"), strict=True)
    )
    assert first.has_failures is False
    assert first.review_required is False


@pytest.mark.asyncio
async def test_blocked_static_page_marks_source_blocked_without_failing_the_run() -> None:
    """One refused product page must report BLOCKED, keep the rest, and not fail the scan."""

    blocked_url = STATIC_PAGE_URLS[1]
    results = {url: _fetch(f"<h1>{url}</h1>".encode(), url=url) for url in STATIC_PAGE_URLS}
    results[blocked_url] = _fetch(b"challenge", status=FetchStatus.BLOCKED, url=blocked_url)
    store = FakeScanStore()
    scanner = CampaignScanner(
        bank_registry=_bank_registry(),
        campaign_registry=_static_campaign_registry(),
        collector=UrlKeyedCollector(results),
        store=store,
    )

    result = await scanner.run("scan-static-blocked")

    assert result.has_blocked_sources is True
    assert result.has_failures is False
    source = result.sources[0]
    assert source.discovered_targets == 2
    assert source.error_code is not None
    assert result.enqueued_jobs == 2
    assert blocked_url not in {job.url for job in store.jobs}


def _mixed_campaign_registry() -> MonitoredCampaignSourceRegistry:
    """One bank monitored through both a campaign index and curated static pages."""

    index_source = MonitoredCampaignSource(
        bank_id="bank-a",
        status=CampaignSourceStatus.VERIFIED,
        observed_on=date(2026, 7, 21),
        evidence_url="https://bank.example/kampanyalar/",
        discovery_mode=DiscoveryMode.DETAIL_LINKS,
        index_url="https://bank.example/kampanyalar/",
        detail_path_prefixes=("/kampanyalar/",),
        max_links=20,
    )
    static_source = MonitoredCampaignSource(
        bank_id="bank-a",
        status=CampaignSourceStatus.VERIFIED,
        observed_on=date(2026, 7, 21),
        evidence_url=STATIC_PAGE_URLS[0],
        discovery_mode=DiscoveryMode.STATIC_PAGES,
        static_pages=tuple(
            MonitoredStaticPage(url=url, label=label)
            for url, label in zip(STATIC_PAGE_URLS, ("konut", "tasit", "ihtiyac"), strict=True)
        ),
    )
    return MonitoredCampaignSourceRegistry(
        schema_version="1.0",
        registry_id="monitored-campaign-sources",
        registry_version="2026-07-21.1",
        source_observed_on=date(2026, 7, 21),
        bank_registry_version="2026-07-18.2",
        sources=(index_source, static_source),
    )


def _mixed_collector(index_html: bytes) -> UrlKeyedCollector:
    results = {url: _fetch(f"<h1>{url}</h1>".encode(), url=url) for url in STATIC_PAGE_URLS}
    results["https://bank.example/kampanyalar/"] = _fetch(index_html)
    return UrlKeyedCollector(results)


@pytest.mark.asyncio
async def test_repeat_scan_with_static_page_siblings_keeps_index_source_green() -> None:
    """Regression: static-page targets persisted by an earlier scan must not make
    the same bank's index source fail its known-target invariant on the next scan."""

    index_html = b'<a href="/kampanyalar/a">A</a><a href="/kampanyalar/b">B</a>'
    store = FakeScanStore()
    scanner = CampaignScanner(
        bank_registry=_bank_registry(),
        campaign_registry=_mixed_campaign_registry(),
        collector=_mixed_collector(index_html),
        store=store,
    )

    first = await scanner.run("scan-mixed-001")
    second = await scanner.run("scan-mixed-002")

    assert first.has_failures is False
    assert second.has_failures is False
    index_first, static_first = first.sources
    index_second, static_second = second.sources
    assert index_first.status.value == "success"
    assert static_first.status.value == "success"
    assert index_second.status.value == "success"
    assert static_second.status.value == "success"
    assert index_second.error_code is None
    assert index_second.source_index_changed is False
    # The second scan re-enqueues every retained target under its own run id.
    assert second.enqueued_jobs == 5
    assert second.deduplicated_jobs == 5


@pytest.mark.asyncio
async def test_repeat_scan_after_index_content_change_succeeds_and_flags_change() -> None:
    store = FakeScanStore()
    collector = _mixed_collector(b'<a href="/kampanyalar/a">A</a>')
    scanner = CampaignScanner(
        bank_registry=_bank_registry(),
        campaign_registry=_mixed_campaign_registry(),
        collector=collector,
        store=store,
    )

    first = await scanner.run("scan-change-001")
    collector.results["https://bank.example/kampanyalar/"] = _fetch(
        b'<a href="/kampanyalar/a">A</a><a href="/kampanyalar/new">New</a>'
    )
    second = await scanner.run("scan-change-002")

    assert first.has_failures is False
    assert second.has_failures is False
    index_second = second.sources[0]
    assert index_second.status.value == "success"
    assert index_second.source_index_changed is True
    assert index_second.discovered_targets == 1
    assert "https://bank.example/kampanyalar/new" in {job.url for job in store.jobs}


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
