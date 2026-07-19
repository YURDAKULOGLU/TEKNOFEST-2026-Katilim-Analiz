"""Resolve image and source-checkout runtime assets without depending on one CWD."""

from __future__ import annotations

from pathlib import Path


class RuntimePathError(FileNotFoundError):
    """A required packaged runtime asset is missing."""


def resolve_runtime_path(relative: str | Path) -> Path:
    """Find a repository/image asset in the supported runtime layouts."""

    value = Path(relative)
    if value.is_absolute():
        candidate = value.resolve()
        if candidate.exists():
            return candidate
        raise RuntimePathError(f"required runtime path does not exist: {candidate}")

    source_root = Path(__file__).resolve().parents[4]
    candidates = (
        Path.cwd() / value,
        Path.cwd().parent / value,
        source_root / value,
    )
    checked: list[Path] = []
    for item in candidates:
        resolved = item.resolve()
        if resolved in checked:
            continue
        checked.append(resolved)
        if resolved.exists():
            return resolved
    raise RuntimePathError(f"required runtime path was not found: {value}")
