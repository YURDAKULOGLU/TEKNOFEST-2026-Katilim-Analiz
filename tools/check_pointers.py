"""Validate the repository's pointer graph without runtime dependencies."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POINTER_FILE = ROOT / "control" / "pointers.json"
GROUP_PREFIXES = {
    "requirements": "REQ",
    "enhancements": "ENH",
    "decisions": "ADR",
    "evaluations": "EVAL",
    "work_packages": "WP",
}
POINTER_PATTERN = re.compile(r"\b(?:REQ|ENH|ADR|EVAL|WP)-\d{3}\b")


def load_graph() -> dict[str, Any]:
    with POINTER_FILE.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("pointer root must be an object")
    return value


def validate() -> list[str]:
    graph = load_graph()
    errors: list[str] = []
    all_ids: set[str] = set()

    for group, prefix in GROUP_PREFIXES.items():
        nodes = graph.get(group)
        if not isinstance(nodes, dict) or not nodes:
            errors.append(f"{group}: expected a non-empty object")
            continue
        for pointer_id, node in nodes.items():
            if not re.fullmatch(rf"{prefix}-\d{{3}}", pointer_id):
                errors.append(f"{pointer_id}: invalid ID for {group}")
            if pointer_id in all_ids:
                errors.append(f"{pointer_id}: duplicate ID")
            all_ids.add(pointer_id)
            if not isinstance(node, dict):
                errors.append(f"{pointer_id}: node must be an object")

    for group in GROUP_PREFIXES:
        for pointer_id, node in graph.get(group, {}).items():
            if not isinstance(node, dict):
                continue
            for key in ("links", "depends_on"):
                for reference in node.get(key, []):
                    if reference not in all_ids:
                        errors.append(
                            f"{pointer_id}: unknown {key} reference {reference}"
                        )
            source = node.get("source")
            if isinstance(source, str) and source.startswith("docs/"):
                source_path = ROOT / source
                if not source_path.is_file():
                    errors.append(f"{pointer_id}: missing source file {source}")

    referenced_in_docs: Counter[str] = Counter()
    for directory in (ROOT / "docs", ROOT / ".Codex"):
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".md", ".json"}:
                referenced_in_docs.update(
                    POINTER_PATTERN.findall(path.read_text(encoding="utf-8"))
                )
    unknown_doc_refs = sorted(set(referenced_in_docs) - all_ids)
    errors.extend(
        f"documentation references unknown pointer {item}" for item in unknown_doc_refs
    )
    return errors


def main() -> int:
    try:
        errors = validate()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"POINTER_GRAPH_ERROR: {exc}", file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(f"POINTER_GRAPH_ERROR: {error}", file=sys.stderr)
        return 1
    print("POINTER_GRAPH_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
