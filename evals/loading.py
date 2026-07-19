"""Strict JSON and JSONL loading without ambiguous duplicate handling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DatasetError(ValueError):
    """A versioned evaluation input is malformed or ambiguous."""


def load_json(path: str | Path) -> Any:
    source = Path(path)
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatasetError(f"cannot load JSON {source}: {exc}") from exc


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    rows: list[dict[str, Any]] = []
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DatasetError(f"cannot read JSONL {source}: {exc}") from exc
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"invalid JSONL {source}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise DatasetError(f"JSONL {source}:{line_number} must contain an object")
        rows.append(value)
    return rows


def load_jsonl_index(path: str | Path, *, id_field: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for line_number, row in enumerate(load_jsonl(path), start=1):
        identifier = row.get(id_field)
        if not isinstance(identifier, str) or not identifier.strip():
            raise DatasetError(f"row {line_number} has invalid {id_field}")
        if identifier in indexed:
            raise DatasetError(f"duplicate {id_field} {identifier!r}")
        indexed[identifier] = row
    return indexed
