"""Bounded, polite candidate-source discovery producing a human-review report.

The runner surfaces NEW candidate product/campaign pages on hosts each bank has
already allowlisted. It only ever writes a dated JSON suggestion report under
``datasets/discovery/``; it never enqueues jobs, never touches the database,
and never edits the monitored campaign registry. Promotion of a suggestion is
an explicit human registry edit, documented in
``docs/operations/aday-kaynak-kesfi.md``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from katilim_analiz.candidates.heuristics import (
    classify_candidate_path,
    internal_links_from_html,
    is_discovery_blocked_bank,
    is_known_url,
    registry_known_urls,
    sitemap_urls_from_robots,
    urls_from_sitemap_xml,
)
from katilim_analiz.config import Settings
from katilim_analiz.ingestion.policy import HostPolicy, PolicyViolation, validate_url_syntax
from katilim_analiz.ingestion.registry import BankRegistry, BankSource
from katilim_analiz.ingestion.robots import robots_response_decision
from katilim_analiz.runtime.composition import RuntimeConfigurationError
from katilim_analiz.runtime.registry import (
    MonitoredCampaignSource,
    MonitoredCampaignSourceRegistry,
    load_monitored_campaign_registry,
    load_runtime_registry,
)

_REPORT_SCHEMA_VERSION = "1.0"
_MAX_SITEMAP_FETCHES_PER_BANK = 6
_MAX_CANDIDATE_POOL_PER_BANK = 3_000
_REQUEST_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class CandidateSuggestion:
    """One ranked suggestion for a human to review; never auto-enrolled."""

    bank_id: str
    url: str
    guessed_label: str
    matched_tokens: tuple[str, ...]
    discovered_via: str
    score: int
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class BankDiscoveryResult:
    bank_id: str
    examined_urls: int
    suggestions: tuple[CandidateSuggestion, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    generated_at: datetime
    bank_registry_version: str
    campaign_registry_version: str
    dry_run: bool
    banks: tuple[BankDiscoveryResult, ...]

    @property
    def suggestion_count(self) -> int:
        return sum(len(bank.suggestions) for bank in self.banks)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": _REPORT_SCHEMA_VERSION,
            "report_id": "candidate-source-suggestions",
            "generated_at": self.generated_at.isoformat(),
            "bank_registry_version": self.bank_registry_version,
            "campaign_registry_version": self.campaign_registry_version,
            "dry_run": self.dry_run,
            "auto_enrollment": "never",
            "suggestion_count": self.suggestion_count,
            "banks": [
                {
                    "bank_id": bank.bank_id,
                    "examined_urls": bank.examined_urls,
                    "notes": list(bank.notes),
                    "suggestions": [
                        {
                            "url": suggestion.url,
                            "guessed_label": suggestion.guessed_label,
                            "matched_tokens": list(suggestion.matched_tokens),
                            "discovered_via": suggestion.discovered_via,
                            "score": suggestion.score,
                            "http_status": suggestion.http_status,
                        }
                        for suggestion in bank.suggestions
                    ],
                }
                for bank in self.banks
            ],
        }


class _PoliteFetcher:
    """Sequential HTTPS fetches with per-host pacing on allowlisted hosts only."""

    def __init__(self, *, user_agent: str, per_host_delay_seconds: float, max_bytes: int) -> None:
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": user_agent},
            max_redirects=5,
        )
        self._per_host_delay_seconds = per_host_delay_seconds
        self._max_bytes = max_bytes
        self._seen_hosts: set[str] = set()
        self._robots: dict[str, tuple[int, bytes]] = {}
        self._product_token = HostPolicy(user_agent=user_agent).robots_product_token

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _pace(self, host: str) -> None:
        if host in self._seen_hosts:
            await asyncio.sleep(self._per_host_delay_seconds)
        self._seen_hosts.add(host)

    async def _request(self, method: str, url: str, bank: BankSource) -> httpx.Response | None:
        canonical, host = validate_url_syntax(url, bank)
        await self._pace(host)
        try:
            response = await self._client.request(method, canonical)
        except httpx.HTTPError:
            return None
        try:
            validate_url_syntax(str(response.url), bank)
        except PolicyViolation:
            return None
        if len(response.content) > self._max_bytes:
            return None
        return response

    async def robots_state(self, host: str, bank: BankSource) -> tuple[int, bytes]:
        if host not in self._robots:
            response = await self._request("GET", f"https://{host}/robots.txt", bank)
            if response is None:
                # Fail closed: an unreachable robots.txt denies collection.
                self._robots[host] = (503, b"")
            else:
                self._robots[host] = (response.status_code, response.content)
        return self._robots[host]

    async def robots_allows(self, url: str, bank: BankSource) -> bool:
        _, host = validate_url_syntax(url, bank)
        status_code, content = await self.robots_state(host, bank)
        decision = robots_response_decision(status_code, content, url, self._product_token)
        return decision.allowed

    async def get_text(self, url: str, bank: BankSource) -> str | None:
        response = await self._request("GET", url, bank)
        if response is None or response.status_code != 200:
            return None
        return response.text

    async def head_status(self, url: str, bank: BankSource) -> int | None:
        response = await self._request("HEAD", url, bank)
        return None if response is None else response.status_code


def _bank_source_urls(sources: tuple[MonitoredCampaignSource, ...]) -> tuple[str, ...]:
    urls: list[str] = []
    for source in sources:
        for url in (
            source.index_url,
            source.evidence_url,
            *(page.url for page in source.static_pages),
        ):
            if url is not None and url not in urls:
                urls.append(url)
    return tuple(urls)


async def _collect_sitemap_urls(
    fetcher: _PoliteFetcher,
    bank: BankSource,
    hosts: tuple[str, ...],
    notes: list[str],
) -> dict[str, str]:
    """Return candidate URL -> discovery channel from robots-declared sitemaps."""

    candidates: dict[str, str] = {}
    fetch_budget = _MAX_SITEMAP_FETCHES_PER_BANK
    for host in hosts:
        status_code, content = await fetcher.robots_state(host, bank)
        declared: tuple[str, ...] = ()
        if 200 <= status_code < 300:
            declared = sitemap_urls_from_robots(content.decode("utf-8", errors="replace"))
        queue = list(declared) or [f"https://{host}/sitemap.xml"]
        while queue and fetch_budget > 0:
            sitemap_url = queue.pop(0)
            try:
                validate_url_syntax(sitemap_url, bank)
            except PolicyViolation:
                notes.append(f"sitemap outside the allowlist was skipped: {sitemap_url}")
                continue
            fetch_budget -= 1
            body = await fetcher.get_text(sitemap_url, bank)
            if body is None:
                notes.append(f"sitemap was unavailable: {sitemap_url}")
                continue
            for loc in urls_from_sitemap_xml(body):
                if loc.casefold().endswith(".xml"):
                    if loc not in queue:
                        queue.append(loc)
                    continue
                try:
                    canonical, _ = validate_url_syntax(loc, bank)
                except (PolicyViolation, ValueError):
                    continue
                if len(candidates) >= _MAX_CANDIDATE_POOL_PER_BANK:
                    return candidates
                candidates.setdefault(canonical, "sitemap")
    return candidates


async def _collect_internal_links(
    fetcher: _PoliteFetcher,
    bank: BankSource,
    monitored_urls: tuple[str, ...],
    candidates: dict[str, str],
    notes: list[str],
) -> None:
    for page_url in monitored_urls:
        if not await fetcher.robots_allows(page_url, bank):
            notes.append(f"robots.txt denies the monitored page: {page_url}")
            continue
        html = await fetcher.get_text(page_url, bank)
        if html is None:
            notes.append(f"monitored page was unavailable: {page_url}")
            continue
        for link in internal_links_from_html(html, base_url=page_url, bank=bank):
            if len(candidates) >= _MAX_CANDIDATE_POOL_PER_BANK:
                return
            candidates.setdefault(link, "internal_link")


async def _discover_bank(
    fetcher: _PoliteFetcher,
    *,
    bank: BankSource,
    sources: tuple[MonitoredCampaignSource, ...],
    dry_run: bool,
    max_candidates: int,
) -> BankDiscoveryResult:
    notes: list[str] = []
    monitored_urls = _bank_source_urls(sources)
    known = registry_known_urls(monitored_urls)
    hosts = tuple(dict.fromkeys(validate_url_syntax(url, bank)[1] for url in monitored_urls))
    candidates = await _collect_sitemap_urls(fetcher, bank, hosts, notes)
    await _collect_internal_links(fetcher, bank, monitored_urls, candidates, notes)

    scored: list[CandidateSuggestion] = []
    for url, discovered_via in candidates.items():
        if is_known_url(url, known):
            continue
        classification = classify_candidate_path(urlsplit(url).path or "/")
        if not classification.is_candidate or classification.guessed_label is None:
            continue
        scored.append(
            CandidateSuggestion(
                bank_id=bank.id,
                url=url,
                guessed_label=classification.guessed_label,
                matched_tokens=classification.matched_tokens,
                discovered_via=discovered_via,
                score=classification.score,
            )
        )
    ranked = sorted(scored, key=lambda item: (-item.score, item.url))[:max_candidates]
    if not dry_run:
        checked: list[CandidateSuggestion] = []
        for suggestion in ranked:
            status: int | None = None
            if await fetcher.robots_allows(suggestion.url, bank):
                status = await fetcher.head_status(suggestion.url, bank)
            else:
                notes.append(f"robots.txt denies the candidate: {suggestion.url}")
            checked.append(
                CandidateSuggestion(
                    bank_id=suggestion.bank_id,
                    url=suggestion.url,
                    guessed_label=suggestion.guessed_label,
                    matched_tokens=suggestion.matched_tokens,
                    discovered_via=suggestion.discovered_via,
                    score=suggestion.score,
                    http_status=status,
                )
            )
        ranked = checked
    return BankDiscoveryResult(
        bank_id=bank.id,
        examined_urls=len(candidates),
        suggestions=tuple(ranked),
        notes=tuple(notes),
    )


def _eligible_banks(
    bank_registry: BankRegistry,
    campaign_registry: MonitoredCampaignSourceRegistry,
) -> tuple[tuple[BankSource, tuple[MonitoredCampaignSource, ...]], ...]:
    eligible: list[tuple[BankSource, tuple[MonitoredCampaignSource, ...]]] = []
    for bank in bank_registry.banks:
        if is_discovery_blocked_bank(bank.id):
            continue
        sources = tuple(
            source for source in campaign_registry.bank_sources(bank.id) if source.verified
        )
        if sources:
            eligible.append((bank, sources))
    return tuple(eligible)


async def run_candidate_discovery(
    settings: Settings,
    *,
    registry_path: str | Path | None = None,
    campaign_registry_path: str | Path | None = None,
    dry_run: bool = False,
    max_candidates_per_bank: int = 30,
) -> DiscoveryReport:
    """Run one bounded discovery pass and return the suggestion report."""

    if not settings.ingest_network_enabled:
        raise RuntimeConfigurationError("candidate discovery requires INGEST_NETWORK_ENABLED=true")
    if not 1 <= max_candidates_per_bank <= 30:
        raise ValueError("max_candidates_per_bank must be within 1..30")
    bank_registry = load_runtime_registry(registry_path)
    campaign_registry = load_monitored_campaign_registry(
        campaign_registry_path,
        bank_registry=bank_registry,
    )
    fetcher = _PoliteFetcher(
        user_agent=settings.ingest_user_agent,
        per_host_delay_seconds=settings.ingest_per_host_delay_seconds,
        max_bytes=settings.ingest_max_bytes,
    )
    results: list[BankDiscoveryResult] = []
    try:
        for bank, sources in _eligible_banks(bank_registry, campaign_registry):
            try:
                results.append(
                    await _discover_bank(
                        fetcher,
                        bank=bank,
                        sources=sources,
                        dry_run=dry_run,
                        max_candidates=max_candidates_per_bank,
                    )
                )
            except Exception as exc:  # one bank must not sink the whole pass
                results.append(
                    BankDiscoveryResult(
                        bank_id=bank.id,
                        examined_urls=0,
                        suggestions=(),
                        notes=(f"discovery failed: {type(exc).__name__}",),
                    )
                )
    finally:
        await fetcher.aclose()
    return DiscoveryReport(
        generated_at=datetime.now(UTC),
        bank_registry_version=bank_registry.registry_version,
        campaign_registry_version=campaign_registry.registry_version,
        dry_run=dry_run,
        banks=tuple(results),
    )


def write_discovery_report(report: DiscoveryReport, output_dir: str | Path) -> Path:
    """Persist the dated suggestion report and return its path."""

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"source-candidates-{report.generated_at.date().isoformat()}.json"
    path.write_text(
        json.dumps(report.to_payload(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path
