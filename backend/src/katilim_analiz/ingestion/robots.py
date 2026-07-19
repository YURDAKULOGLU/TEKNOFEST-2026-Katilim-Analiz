"""RFC 9309 robots.txt parsing, status semantics, and cache hooks."""

from __future__ import annotations

import asyncio
import re
import string
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import quote, urlsplit

MAX_ROBOTS_BYTES = 512_000
_HEX = frozenset(string.hexdigits)
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    allowed: bool
    reason: str
    matched_user_agent: str | None = None
    matched_rule: str | None = None
    crawl_delay_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class RobotsSnapshot:
    source_url: str
    status_code: int
    content: bytes
    fetched_at: datetime
    expires_at: datetime


class RobotsCache(Protocol):
    async def get(self, key: str, now: datetime) -> RobotsSnapshot | None: ...

    async def put(self, key: str, snapshot: RobotsSnapshot) -> None: ...


class InMemoryRobotsCache:
    def __init__(self) -> None:
        self._items: dict[str, RobotsSnapshot] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str, now: datetime) -> RobotsSnapshot | None:
        async with self._lock:
            snapshot = self._items.get(key)
            if snapshot is not None and snapshot.expires_at <= now:
                del self._items[key]
                return None
            return snapshot

    async def put(self, key: str, snapshot: RobotsSnapshot) -> None:
        async with self._lock:
            self._items[key] = snapshot


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    pattern: str
    specificity: int


@dataclass(frozen=True, slots=True)
class _Group:
    agents: tuple[str, ...]
    rules: tuple[_Rule, ...]
    crawl_delay: float | None


def _normalize_octets(value: str, *, keep_patterns: bool) -> str:
    normalized: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if (
            character == "%"
            and index + 2 < len(value)
            and value[index + 1] in _HEX
            and value[index + 2] in _HEX
        ):
            octet = int(value[index + 1 : index + 3], 16)
            decoded = chr(octet)
            normalized.append(decoded if decoded in _UNRESERVED else f"%{octet:02X}")
            index += 3
            continue
        literal_character = character in _UNRESERVED or character in "/?=&;:+,@"
        if literal_character or (keep_patterns and character in "*$"):
            normalized.append(character)
        else:
            normalized.append(quote(character, safe=""))
        index += 1
    return "".join(normalized)


def _parse_groups(content: str) -> tuple[_Group, ...]:
    groups: list[_Group] = []
    agents: list[str] = []
    rules: list[_Rule] = []
    crawl_delay: float | None = None
    directives_started = False

    def finish_group() -> None:
        nonlocal agents, rules, crawl_delay, directives_started
        if agents:
            groups.append(_Group(tuple(agents), tuple(rules), crawl_delay))
        agents = []
        rules = []
        crawl_delay = None
        directives_started = False

    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, raw_value = line.split(":", 1)
        field = field.strip().casefold()
        value = raw_value.strip()
        if field == "user-agent":
            if directives_started:
                finish_group()
            if value:
                agents.append(value.casefold())
            continue
        if not agents:
            continue
        if field in {"allow", "disallow"}:
            directives_started = True
            if not value:
                continue
            pattern = _normalize_octets(value, keep_patterns=True)
            specificity = _pattern_specificity(pattern)
            rules.append(_Rule(field == "allow", pattern, specificity))
        elif field == "crawl-delay":
            directives_started = True
            try:
                parsed_delay = float(value)
            except ValueError:
                continue
            if 0 <= parsed_delay <= 86_400:
                crawl_delay = parsed_delay
    finish_group()
    return tuple(groups)


def _agent_specificity(agent: str, product_token: str) -> int | None:
    if agent == "*":
        return 0
    normalized_token = product_token.casefold()
    return len(agent) if agent == normalized_token else None


def _pattern_specificity(pattern: str) -> int:
    """Count matched octets, treating each percent-encoded octet as one."""

    value = pattern.removesuffix("$").replace("*", "")
    specificity = 0
    index = 0
    while index < len(value):
        encoded_octet = (
            value[index] == "%"
            and index + 2 < len(value)
            and value[index + 1] in _HEX
            and value[index + 2] in _HEX
        )
        specificity += 1
        index += 3 if encoded_octet else 1
    return specificity


def _rule_matches(pattern: str, path_query: str) -> bool:
    end_anchored = pattern.endswith("$")
    expression = re.escape(pattern.removesuffix("$"))
    expression = expression.replace(r"\*", ".*")
    if end_anchored:
        expression += "$"
    return re.match(expression, path_query) is not None


def evaluate_robots(content: str, target_url: str, product_token: str) -> RobotsDecision:
    """Evaluate a decoded robots file using RFC 9309 group and rule precedence."""

    groups = _parse_groups(content)
    matches: list[tuple[int, _Group, str]] = []
    for group in groups:
        matching_agents = [
            (specificity, agent)
            for agent in group.agents
            if (specificity := _agent_specificity(agent, product_token)) is not None
        ]
        if matching_agents:
            specificity, agent = max(matching_agents, key=lambda item: item[0])
            matches.append((specificity, group, agent))
    if not matches:
        return RobotsDecision(True, "robots_no_matching_group")
    best_agent_specificity = max(item[0] for item in matches)
    selected = [item for item in matches if item[0] == best_agent_specificity]
    rules = [rule for _, group, _ in selected for rule in group.rules]
    delays = [group.crawl_delay for _, group, _ in selected if group.crawl_delay is not None]
    crawl_delay = max(delays, default=None)
    parsed_url = urlsplit(target_url)
    raw_path_query = parsed_url.path or "/"
    if parsed_url.query:
        raw_path_query += f"?{parsed_url.query}"
    path_query = _normalize_octets(raw_path_query, keep_patterns=False)
    matching_rules = [rule for rule in rules if _rule_matches(rule.pattern, path_query)]
    matched_agent = selected[0][2]
    if not matching_rules:
        return RobotsDecision(
            True,
            "robots_allowed",
            matched_user_agent=matched_agent,
            crawl_delay_seconds=crawl_delay,
        )
    max_specificity = max(rule.specificity for rule in matching_rules)
    most_specific = [rule for rule in matching_rules if rule.specificity == max_specificity]
    winning_rule = next((rule for rule in most_specific if rule.allow), most_specific[0])
    return RobotsDecision(
        winning_rule.allow,
        "robots_allowed" if winning_rule.allow else "robots_disallowed",
        matched_user_agent=matched_agent,
        matched_rule=winning_rule.pattern,
        crawl_delay_seconds=crawl_delay,
    )


def robots_response_decision(
    status_code: int,
    content: bytes,
    target_url: str,
    product_token: str,
) -> RobotsDecision:
    """Apply RFC 9309 access semantics to a terminal robots response."""

    if status_code in {401, 403}:
        return RobotsDecision(False, "robots_access_denied")
    if 400 <= status_code < 500:
        return RobotsDecision(True, "robots_unavailable")
    if not 200 <= status_code < 300:
        return RobotsDecision(False, "robots_unreachable")
    if len(content) > MAX_ROBOTS_BYTES:
        return RobotsDecision(False, "robots_too_large")
    try:
        decoded = content.decode("utf-8", errors="strict").lstrip("\ufeff")
    except UnicodeDecodeError:
        return RobotsDecision(False, "robots_invalid_utf8")
    return evaluate_robots(decoded, target_url, product_token)
