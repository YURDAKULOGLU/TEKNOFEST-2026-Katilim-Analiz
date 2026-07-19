from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from katilim_analiz.auth.passwords import (
    BLOCKLIST_COVERAGE_CONTRACT,
    BlocklistUnavailableError,
    PasswordBlocklist,
    PasswordPolicyError,
    normalize_password,
    validate_bootstrap_password,
    validate_password_shape,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_ENTRIES = _REPOSITORY_ROOT / "data/security/admin-password-blocklist-v1.txt"
_METADATA = _REPOSITORY_ROOT / "data/security/admin-password-blocklist-v1.metadata.json"


def test_versioned_blocklist_verifies_its_bounded_coverage_contract() -> None:
    blocklist = PasswordBlocklist.load(_ENTRIES, _METADATA)
    metadata = json.loads(_METADATA.read_text(encoding="utf-8"))

    assert blocklist.list_version == "2026-07-19.1"
    assert metadata["coverage"] == BLOCKLIST_COVERAGE_CONTRACT
    assert metadata["entry_count"] == len(blocklist.entries) == 28
    assert metadata["content_sha256"] == hashlib.sha256(_ENTRIES.read_bytes()).hexdigest()


def test_password_policy_uses_nfc_codepoints_without_trim_or_case_composition() -> None:
    decomposed = "e\u0301" + "x" * 14
    normalized = validate_password_shape(decomposed)

    assert normalized == "é" + "x" * 14
    assert len(normalized) == 15
    assert normalize_password("  Keep My Spaces  ") == "  Keep My Spaces  "

    with pytest.raises(PasswordPolicyError) as too_short:
        validate_password_shape("x" * 14)
    assert too_short.value.code == "too_short"

    with pytest.raises(PasswordPolicyError) as too_long:
        validate_password_shape("x" * 129)
    assert too_long.value.code == "too_long"


def test_blocklist_matches_only_the_complete_nfc_casefold_value() -> None:
    blocklist = PasswordBlocklist.load(_ENTRIES, _METADATA)

    with pytest.raises(PasswordPolicyError) as blocked:
        validate_bootstrap_password("TEKNOFEST2026ADMIN", blocklist)
    assert blocked.value.code == "blocked_value"
    assert "TEKNOFEST2026ADMIN" not in str(blocked.value)

    assert (
        validate_bootstrap_password("prefix-teknofest2026admin-suffix", blocklist)
        == "prefix-teknofest2026admin-suffix"
    )
    assert validate_bootstrap_password(" teknofest2026admin ", blocklist) == " teknofest2026admin "


@pytest.mark.parametrize("missing", ["entries", "metadata"])
def test_blocklist_loading_fails_closed_when_a_file_is_missing(
    tmp_path: Path, missing: str
) -> None:
    entries = tmp_path / "list.txt"
    metadata = tmp_path / "list.json"
    if missing != "entries":
        entries.write_text("a-valid-context-password\n", encoding="utf-8")
    if missing != "metadata":
        metadata.write_text("{}", encoding="utf-8")

    with pytest.raises(BlocklistUnavailableError, match="unavailable"):
        PasswordBlocklist.load(entries, metadata)


def test_blocklist_loading_fails_closed_on_content_or_contract_drift(tmp_path: Path) -> None:
    entries = tmp_path / "list.txt"
    metadata = tmp_path / "list.json"
    entries.write_bytes(_ENTRIES.read_bytes() + b"unexpected-password\n")
    metadata.write_bytes(_METADATA.read_bytes())

    with pytest.raises(BlocklistUnavailableError, match="hash does not match"):
        PasswordBlocklist.load(entries, metadata)

    entries.write_bytes(_ENTRIES.read_bytes())
    value = json.loads(_METADATA.read_text(encoding="utf-8"))
    value["coverage"] = "breached-password-corpus"
    metadata.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(BlocklistUnavailableError, match="coverage claim"):
        PasswordBlocklist.load(entries, metadata)
