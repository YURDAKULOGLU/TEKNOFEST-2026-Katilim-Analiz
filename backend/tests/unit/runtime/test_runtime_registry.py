from __future__ import annotations

import pytest

from katilim_analiz.ingestion.policy import PolicyViolation
from katilim_analiz.runtime.registry import build_source_request, load_runtime_registry


def test_runtime_registry_is_the_versioned_ten_bank_allowlist() -> None:
    registry = load_runtime_registry()
    assert registry.registry_version == "2026-07-18.2"
    assert len(registry.banks) == 10


def test_source_request_is_canonical_and_registry_bound() -> None:
    registry = load_runtime_registry()
    source = build_source_request(
        registry,
        bank_id="dunya-katilim",
        source_url="https://dunyakatilim.com.tr/kampanyalar/network#ignored",
    )
    assert str(source.source_url) == "https://dunyakatilim.com.tr/kampanyalar/network"
    assert source.bank_name == registry.bank("dunya-katilim").legal_name
    assert source.campaign_key.startswith("dunya-katilim:")

    with pytest.raises(PolicyViolation, match="not approved"):
        build_source_request(
            registry,
            bank_id="dunya-katilim",
            source_url="https://attacker.example/kampanya",
        )
