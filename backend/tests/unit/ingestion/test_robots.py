from __future__ import annotations

import pytest

from katilim_analiz.ingestion.robots import evaluate_robots, robots_response_decision

ROBOTS = """
User-agent: *
Disallow: /

User-agent: OtherBot
Disallow: /other

User-agent: KatilimAnalizBot
Disallow: /private
Allow: /private/public
Disallow: /*.pdf$
Crawl-delay: 3.5

User-agent: KatilimAnalizBot
Disallow: /also-private
"""


@pytest.mark.parametrize(
    ("url", "allowed", "rule"),
    [
        ("https://bank.example/campaigns", True, None),
        ("https://bank.example/private/item", False, "/private"),
        ("https://bank.example/private/public/item", True, "/private/public"),
        ("https://bank.example/report.pdf", False, "/*.pdf$"),
        ("https://bank.example/report.pdf?download=1", True, None),
        ("https://bank.example/also-private/item", False, "/also-private"),
        ("https://bank.example/Private/item", True, None),
    ],
)
def test_rfc9309_group_selection_and_longest_rule_wins(
    url: str, allowed: bool, rule: str | None
) -> None:
    decision = evaluate_robots(ROBOTS, url, "KatilimAnalizBot")

    assert decision.allowed is allowed
    assert decision.matched_rule == rule
    assert decision.crawl_delay_seconds == 3.5


def test_allow_wins_when_rules_have_equal_specificity() -> None:
    content = """
User-agent: *
Disallow: /same
Allow: /same
"""

    assert evaluate_robots(content, "https://bank.example/same", "AnyBot").allowed


def test_partial_user_agent_name_does_not_override_the_wildcard_group() -> None:
    content = """
User-agent: *
Disallow: /wildcard

User-agent: AnalizBot
Allow: /
"""

    decision = evaluate_robots(
        content,
        "https://bank.example/wildcard",
        "KatilimAnalizBot",
    )

    assert not decision.allowed
    assert decision.matched_user_agent == "*"


def test_percent_encoded_unreserved_octets_are_normalized() -> None:
    content = "User-agent: *\nDisallow: /campaign%73/private\n"

    decision = evaluate_robots(content, "https://bank.example/campaigns/private", "AnyBot")

    assert not decision.allowed


@pytest.mark.parametrize(
    ("status", "allowed", "reason"),
    [
        (401, False, "robots_access_denied"),
        (403, False, "robots_access_denied"),
        (404, True, "robots_unavailable"),
        (410, True, "robots_unavailable"),
        (500, False, "robots_unreachable"),
        (503, False, "robots_unreachable"),
    ],
)
def test_rfc9309_response_status_policy(status: int, allowed: bool, reason: str) -> None:
    decision = robots_response_decision(
        status,
        b"",
        "https://bank.example/campaign",
        "KatilimAnalizBot",
    )

    assert decision.allowed is allowed
    assert decision.reason == reason


def test_oversized_or_non_utf8_robots_content_fails_closed() -> None:
    oversized = b"User-agent: *\nAllow: /\n" + b" " * 512_001
    decision = robots_response_decision(
        200,
        oversized,
        "https://bank.example/campaign",
        "KatilimAnalizBot",
    )
    invalid_utf8 = robots_response_decision(
        200,
        b"User-agent: *\nAllow: /\xff\n",
        "https://bank.example/campaign",
        "KatilimAnalizBot",
    )

    assert not decision.allowed
    assert decision.reason == "robots_too_large"
    assert not invalid_utf8.allowed
    assert invalid_utf8.reason == "robots_invalid_utf8"
