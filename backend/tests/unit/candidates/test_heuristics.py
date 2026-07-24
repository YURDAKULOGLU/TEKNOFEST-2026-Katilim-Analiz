"""Deterministic tests for candidate-source discovery heuristics (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from katilim_analiz.candidates import (
    DISCOVERY_BLOCKED_BANK_IDS,
    classify_candidate_path,
    internal_links_from_html,
    is_discovery_blocked_bank,
    registry_known_urls,
    sitemap_urls_from_robots,
    urls_from_sitemap_xml,
)
from katilim_analiz.candidates.heuristics import is_known_url
from katilim_analiz.candidates.runner import _eligible_banks
from katilim_analiz.ingestion.registry import BankSource
from katilim_analiz.runtime.registry import (
    load_monitored_campaign_registry,
    load_runtime_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
BANK_REGISTRY = PROJECT_ROOT / "data/registry/bddk-participation-banks-2026-07-18.json"
CAMPAIGN_REGISTRY = PROJECT_ROOT / "data/registry/monitored-campaign-sources-2026-07-24.json"


def _bank() -> BankSource:
    return BankSource(
        id="bank-a",
        listing_order=1,
        legal_name="Banka A",
        listed_homepage_url="https://bank.example",
        allowed_hosts=("bank.example",),
        digital_bank=False,
    )


class TestClassification:
    @pytest.mark.parametrize(
        ("path", "label", "tokens"),
        [
            ("/bireysel/finansmanlar/konut-finansmani", "konut", ("finansman", "konut")),
            ("/kendim-icin/arac-finansmanlari/arac-finansmani", "tasit", ("arac", "finansman")),
            ("/bireysel/tasit-finansmani", "tasit", ("finansman", "tasit")),
            ("/finansmanlar/ihtiyac", "ihtiyac", ("finansman", "ihtiyac")),
            ("/tr/kampanyalar/detay/yeni-firsat", "kampanya", ("kampanya",)),
            ("/kart-kampanyalari/taksit-firsati", "kampanya", ("kampanya", "kart")),
            ("/urunler/finansman", "finansman", ("finansman",)),
        ],
    )
    def test_positive_slugs(self, path: str, label: str, tokens: tuple[str, ...]) -> None:
        classification = classify_candidate_path(path)
        assert classification.is_candidate
        assert classification.guessed_label == label
        assert classification.matched_tokens == tokens

    @pytest.mark.parametrize(
        "path",
        [
            "/hakkimizda",
            "/iletisim",
            "/insan-kaynaklari/kariyer",
            "/subeler/istanbul-kartal",
            "/yatirimci-iliskileri/finansal-raporlar",
            "/aracilik-hizmetleri",
            "/",
        ],
    )
    def test_negative_slugs(self, path: str) -> None:
        classification = classify_candidate_path(path)
        assert not classification.is_candidate
        assert classification.guessed_label is None
        assert classification.matched_tokens == ()

    def test_turkish_plural_suffixes_match(self) -> None:
        assert classify_candidate_path("/kampanyalari").matched_tokens == ("kampanya",)
        assert classify_candidate_path("/finansmanlari").matched_tokens == ("finansman",)
        assert classify_candidate_path("/araclar").matched_tokens == ("arac",)

    def test_product_tokens_outrank_generic_tokens(self) -> None:
        product = classify_candidate_path("/finansmanlar/konut-finansmani")
        generic = classify_candidate_path("/kampanyalar/detay/firsat")
        assert product.score > generic.score

    def test_rejects_non_string_path(self) -> None:
        with pytest.raises(TypeError):
            classify_candidate_path(None)  # type: ignore[arg-type]


class TestBlockedBanks:
    def test_blocked_set_matches_the_intake_decision(self) -> None:
        assert {
            "adil-katilim",
            "hayat-finans",
            "kuveyt-turk",
            "turkiye-finans",
        } == DISCOVERY_BLOCKED_BANK_IDS

    def test_blocked_and_active_banks(self) -> None:
        assert is_discovery_blocked_bank("kuveyt-turk")
        assert not is_discovery_blocked_bank("albaraka-turk")

    def test_eligible_banks_exclude_every_blocked_bank(self) -> None:
        bank_registry = load_runtime_registry(BANK_REGISTRY)
        campaign_registry = load_monitored_campaign_registry(
            CAMPAIGN_REGISTRY, bank_registry=bank_registry
        )
        eligible = _eligible_banks(bank_registry, campaign_registry)
        eligible_ids = {bank.id for bank, _ in eligible}
        assert eligible_ids.isdisjoint(DISCOVERY_BLOCKED_BANK_IDS)
        assert eligible_ids == {
            "albaraka-turk",
            "dunya-katilim",
            "tom-katilim",
            "emlak-katilim",
            "vakif-katilim",
            "ziraat-katilim",
        }
        for _, sources in eligible:
            assert all(source.verified for source in sources)


class TestRegistryExclusion:
    def test_known_urls_ignore_trailing_slash_variants(self) -> None:
        known = registry_known_urls(
            [
                "https://bank.example/kampanyalar/",
                "https://bank.example/finansmanlar/konut-finansmani",
            ]
        )
        assert is_known_url("https://bank.example/kampanyalar", known)
        assert is_known_url("https://bank.example/kampanyalar/", known)
        assert is_known_url("https://bank.example/finansmanlar/konut-finansmani/", known)
        assert not is_known_url("https://bank.example/finansmanlar/tasit-finansmani", known)

    def test_registry_pages_are_excluded_from_suggestions(self) -> None:
        bank_registry = load_runtime_registry(BANK_REGISTRY)
        campaign_registry = load_monitored_campaign_registry(
            CAMPAIGN_REGISTRY, bank_registry=bank_registry
        )
        monitored = [
            url
            for source in campaign_registry.sources
            for url in (
                source.index_url,
                source.evidence_url,
                *(page.url for page in source.static_pages),
            )
            if url is not None
        ]
        known = registry_known_urls(monitored)
        assert all(is_known_url(url, known) for url in monitored)


class TestSitemapParsing:
    def test_sitemap_directives_from_robots(self) -> None:
        robots = (
            "User-agent: *\n"
            "Disallow: /internet-sube/\n"
            "SITEMAP: https://bank.example/sitemap.xml\n"
            "sitemap: https://bank.example/kampanya-sitemap.xml # yorum\n"
            "Sitemap: https://bank.example/sitemap.xml\n"
        )
        assert sitemap_urls_from_robots(robots) == (
            "https://bank.example/sitemap.xml",
            "https://bank.example/kampanya-sitemap.xml",
        )

    def test_robots_without_sitemap_yields_nothing(self) -> None:
        assert sitemap_urls_from_robots("User-agent: *\nDisallow:\n") == ()

    def test_locs_from_sitemap_xml(self) -> None:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            "  <url><loc> https://bank.example/finansmanlar/konut-finansmani </loc></url>\n"
            "  <url><loc>https://bank.example/hakkimizda</loc></url>\n"
            "  <url><loc>https://bank.example/hakkimizda</loc></url>\n"
            "</urlset>\n"
        )
        assert urls_from_sitemap_xml(xml) == (
            "https://bank.example/finansmanlar/konut-finansmani",
            "https://bank.example/hakkimizda",
        )


class TestInternalLinks:
    def test_only_allowlisted_same_host_links_survive(self) -> None:
        html = (
            '<a href="/finansmanlar/tasit-finansmani">Tasit</a>'
            '<a href="https://bank.example/kampanyalar/yeni">Yeni</a>'
            '<a href="https://evil.example/finansmanlar/konut">Dis</a>'
            '<a href="http://bank.example/duz-http">HTTP</a>'
            '<a href="mailto:info@bank.example">Posta</a>'
            '<a href="/finansmanlar/tasit-finansmani">Tekrar</a>'
        )
        links = internal_links_from_html(
            html, base_url="https://bank.example/kampanyalar", bank=_bank()
        )
        assert links == (
            "https://bank.example/finansmanlar/tasit-finansmani",
            "https://bank.example/kampanyalar/yeni",
        )
