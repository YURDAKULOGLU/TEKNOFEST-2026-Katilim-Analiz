from __future__ import annotations

import hashlib

from katilim_analiz.ingestion.discovery import DiscoveryMode, discover_campaign_index
from katilim_analiz.ingestion.registry import BankSource


def _bank() -> BankSource:
    return BankSource(
        id="bank-a",
        listing_order=1,
        legal_name="Banka A",
        listed_homepage_url="https://bank.example",
        allowed_hosts=("bank.example",),
        digital_bank=False,
    )


def test_discovery_accepts_only_canonical_same_host_detail_anchors() -> None:
    html = b"""
    <html><head>
      <link href="https://bank.example/kampanyalar/not-an-anchor">
    </head><body>
      <a href="/kampanyalar/zeta#terms">Zeta</a>
      <a href="https://bank.example:443/kampanyalar/zeta">duplicate</a>
      <a href="/kampanyalar/alpha?view=full">Alpha</a>
      <a href="https://attacker.example/kampanyalar/stolen">external</a>
      <a href="https://user:secret@bank.example/kampanyalar/credential">credential</a>
      <a href="https://bank.example:8443/kampanyalar/port">port</a>
      <a href="https://127.0.0.1/kampanyalar/private">private</a>
      <a href="javascript:alert(1)">script</a>
      <a href="mailto:test@bank.example">mail</a>
      <a href="/kampanyalar-evil/boundary">prefix boundary</a>
      <a href="/kampanyalar/../admin">traversal</a>
      <a href="/kampanyalar/%2e%2e/admin">encoded traversal</a>
      <a href="/kampanyalar/%252e%252e/admin">double encoded traversal</a>
      <a href="/kampanyalar/#self">collection page</a>
      <a>No href</a>
      <form action="/kampanyalar/not-an-anchor"></form>
      https://bank.example/kampanyalar/plain-text
    </body></html>
    """

    result = discover_campaign_index(
        html,
        bank=_bank(),
        index_url="https://bank.example/kampanyalar/",
        detail_path_prefixes=("/kampanyalar/",),
        max_links=20,
        known_targets=("https://bank.example/kampanyalar/zeta",),
        previous_index_sha256="0" * 64,
    )

    assert result.discovered_detail_targets == ("https://bank.example/kampanyalar/alpha?view=full",)
    assert result.unchanged_targets == ("https://bank.example/kampanyalar/zeta",)
    assert result.source_index_changed is True
    assert result.review_required is False
    assert result.index_sha256 == hashlib.sha256(html).hexdigest()


def test_discovery_is_deterministic_deduplicated_and_hard_bounded() -> None:
    first = b"".join(
        f'<a href="/kampanyalar/{number}">{number}</a>'.encode() for number in (5, 3, 1, 4, 2, 1)
    )
    second = b"".join(
        f'<a href="/kampanyalar/{number}#{number}">{number}</a>'.encode()
        for number in (2, 4, 1, 5, 3)
    )

    first_result = discover_campaign_index(
        first,
        bank=_bank(),
        index_url="https://bank.example/kampanyalar/",
        detail_path_prefixes=("/kampanyalar/",),
        max_links=3,
    )
    second_result = discover_campaign_index(
        second,
        bank=_bank(),
        index_url="https://bank.example/kampanyalar/",
        detail_path_prefixes=("/kampanyalar/",),
        max_links=3,
    )

    expected = (
        "https://bank.example/kampanyalar/1",
        "https://bank.example/kampanyalar/2",
        "https://bank.example/kampanyalar/3",
    )
    assert first_result.discovered_detail_targets == expected
    assert second_result.discovered_detail_targets == expected
    assert len(first_result.all_detail_targets) == 3


def test_unsegmented_collection_change_requires_review_but_is_not_a_campaign_target() -> None:
    html = b"<main><h2>Campaign A</h2><h2>Campaign B</h2></main>"

    baseline = discover_campaign_index(
        html,
        bank=_bank(),
        index_url="https://bank.example/kampanyalar.html",
        detail_path_prefixes=(),
        max_links=0,
        mode=DiscoveryMode.UNSEGMENTED_COLLECTION,
    )
    changed = discover_campaign_index(
        html,
        bank=_bank(),
        index_url="https://bank.example/kampanyalar.html",
        detail_path_prefixes=(),
        max_links=0,
        mode=DiscoveryMode.UNSEGMENTED_COLLECTION,
        previous_index_sha256="f" * 64,
    )

    assert baseline.source_index_changed is False
    assert baseline.review_required is False
    assert baseline.all_detail_targets == ()
    assert changed.source_index_changed is True
    assert changed.review_required is True
    assert changed.discovered_detail_targets == ()
    assert changed.unchanged_targets == ()


def test_unchanged_index_hash_does_not_request_review() -> None:
    html = b"<a href='/kampanyalar/one'>One</a>"
    digest = hashlib.sha256(html).hexdigest()

    result = discover_campaign_index(
        html,
        bank=_bank(),
        index_url="https://bank.example/kampanyalar/",
        detail_path_prefixes=("/kampanyalar/",),
        max_links=5,
        previous_index_sha256=digest,
    )

    assert result.source_index_changed is False
    assert result.review_required is False
