"""Deterministic cross-split duplicate and near-duplicate detection."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class SplitLeak:
    left_id: str
    right_id: str
    left_split: str
    right_split: str
    similarity: float
    reason_code: str


def _tokens(text: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return frozenset(re.findall(r"\w+", normalized, flags=re.UNICODE))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def find_split_leakage(
    examples: Sequence[Mapping[str, object]], *, threshold: float = 0.85
) -> tuple[SplitLeak, ...]:
    """Return stable cross-split group/hash/near-text collisions."""

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between zero and one")
    ordered = sorted(examples, key=lambda item: str(item.get("example_id", "")))
    leaks: list[SplitLeak] = []
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            left_split = str(left.get("split", ""))
            right_split = str(right.get("split", ""))
            if not left_split or not right_split or left_split == right_split:
                continue
            reason: str | None = None
            similarity = 0.0
            if left.get("duplicate_group") and left.get("duplicate_group") == right.get(
                "duplicate_group"
            ):
                reason = "duplicate_group_cross_split"
                similarity = 1.0
            elif left.get("source_sha256") and left.get("source_sha256") == right.get(
                "source_sha256"
            ):
                reason = "source_hash_cross_split"
                similarity = 1.0
            else:
                similarity = _jaccard(
                    _tokens(str(left.get("source_excerpt", ""))),
                    _tokens(str(right.get("source_excerpt", ""))),
                )
                if similarity >= threshold:
                    reason = "near_duplicate_cross_split"
            if reason is not None:
                leaks.append(
                    SplitLeak(
                        left_id=str(left.get("example_id", "")),
                        right_id=str(right.get("example_id", "")),
                        left_split=left_split,
                        right_split=right_split,
                        similarity=similarity,
                        reason_code=reason,
                    )
                )
    return tuple(leaks)
