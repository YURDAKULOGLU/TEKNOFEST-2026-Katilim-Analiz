from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from katilim_analiz.ingestion.registry import RegistryValidationError, load_registry

REGISTRY_PATH = (
    Path(__file__).resolve().parents[4]
    / "data"
    / "registry"
    / "bddk-participation-banks-2026-07-18.json"
)
REDIRECT_EVIDENCE_PATH = REGISTRY_PATH.parent / "official-redirects-2026-07-18.json"


def test_registry_is_the_versioned_ten_bank_bddk_observation() -> None:
    registry = load_registry(REGISTRY_PATH)

    assert registry.registry_version == "2026-07-18.2"
    assert registry.source_observed_on.isoformat() == "2026-07-18"
    assert registry.source_url == "https://www.bddk.org.tr/Kurulus/Liste/77"
    assert [bank.listing_order for bank in registry.banks] == list(range(1, 11))
    assert [bank.allowed_hosts for bank in registry.banks] == [
        ("adilkatilim.com.tr", "www.adilkatilim.com.tr"),
        (
            "albarakaturk.com.tr",
            "www.albarakaturk.com.tr",
            "www.albaraka.com.tr",
        ),
        ("dunyakatilim.com.tr", "www.dunyakatilim.com.tr"),
        ("hayatfinans.com.tr", "www.hayatfinans.com.tr"),
        ("kuveytturk.com.tr", "www.kuveytturk.com.tr"),
        ("tombank.com.tr", "www.tombank.com.tr"),
        (
            "emlakbank.com.tr",
            "www.emlakbank.com.tr",
            "www.emlakkatilim.com.tr",
        ),
        ("turkiyefinans.com.tr", "www.turkiyefinans.com.tr"),
        ("vakifkatilim.com.tr", "www.vakifkatilim.com.tr"),
        ("ziraatkatilim.com.tr", "www.ziraatkatilim.com.tr"),
    ]
    assert [bank.legal_name for bank in registry.banks] == [
        "ADİL KATILIM BANKASI A.Ş.",
        "ALBARAKA TÜRK KATILIM BANKASI A.Ş.",
        "DÜNYA KATILIM BANKASI A.Ş.",
        "HAYAT FİNANS KATILIM BANKASI A.Ş.",
        "KUVEYT TÜRK KATILIM BANKASI A.Ş.",
        "T.O.M. KATILIM BANKASI A.Ş.",
        "TÜRKİYE EMLAK KATILIM BANKASI A.Ş.",
        "TÜRKİYE FİNANS KATILIM BANKASI A.Ş.",
        "VAKIF KATILIM BANKASI A.Ş.",
        "ZİRAAT KATILIM BANKASI A.Ş.",
    ]


def test_registry_json_satisfies_its_published_schema() -> None:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    schema = json.loads(
        (REGISTRY_PATH.parent / "bddk-participation-banks.schema.json").read_text(encoding="utf-8")
    )

    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(payload)) == []


def test_canonical_host_additions_have_versioned_primary_redirect_evidence() -> None:
    payload = json.loads(REDIRECT_EVIDENCE_PATH.read_text(encoding="utf-8"))
    schema = json.loads(
        (REDIRECT_EVIDENCE_PATH.parent / "official-redirects.schema.json").read_text(
            encoding="utf-8"
        )
    )

    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(payload)) == []
    assert payload["registry_version"] == "2026-07-18.2"
    assert payload["registry_source_url"] == "https://www.bddk.org.tr/Kurulus/Liste/77"
    assert {row["bank_id"]: row["canonical_hosts_added"] for row in payload["observations"]} == {
        "albaraka-turk": ["www.albaraka.com.tr"],
        "emlak-katilim": ["www.emlakkatilim.com.tr"],
    }


def test_registry_supports_exact_bank_and_host_lookups() -> None:
    registry = load_registry(REGISTRY_PATH)

    bank = registry.bank("kuveyt-turk")
    assert bank.permits_host("WWW.KUVEYTTURK.COM.TR.")
    assert registry.bank_for_host("kuveytturk.com.tr") == bank
    assert not bank.permits_host("campaigns.kuveytturk.com.tr")
    assert not bank.permits_host("kuveytturk.com.tr.attacker.example")


@pytest.mark.parametrize(
    ("bank_id", "allowed_host", "rejected_hosts"),
    [
        (
            "albaraka-turk",
            "www.albaraka.com.tr",
            (
                "albaraka.com.tr",
                "campaigns.albaraka.com.tr",
                "www.albaraka.com.tr.attacker.example",
                "evilalbaraka.com.tr",
            ),
        ),
        (
            "emlak-katilim",
            "www.emlakkatilim.com.tr",
            (
                "emlakkatilim.com.tr",
                "campaigns.emlakkatilim.com.tr",
                "www.emlakkatilim.com.tr.attacker.example",
                "evilemlakkatilim.com.tr",
            ),
        ),
    ],
)
def test_redirect_hosts_are_exactly_scoped(
    bank_id: str,
    allowed_host: str,
    rejected_hosts: tuple[str, ...],
) -> None:
    bank = load_registry(REGISTRY_PATH).bank(bank_id)

    assert bank.permits_host(allowed_host)
    assert all(not bank.permits_host(host) for host in rejected_hosts)


def test_registry_rejects_duplicate_ids_and_count_drift(tmp_path: Path) -> None:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    payload["banks"][1]["id"] = payload["banks"][0]["id"]
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryValidationError, match="bank IDs"):
        load_registry(path)

    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    payload["bank_count"] = 9
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryValidationError, match="bank_count"):
        load_registry(path)

    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    payload["banks"] = payload["banks"][:9]
    payload["bank_count"] = 9
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryValidationError, match="exactly ten"):
        load_registry(path)


def test_registry_rejects_unknown_fields_and_unlisted_homepage_host(tmp_path: Path) -> None:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryValidationError, match="unknown fields"):
        load_registry(path)

    payload.pop("unexpected")
    payload["banks"][0]["allowed_hosts"] = ["example.com"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryValidationError, match="homepage host"):
        load_registry(path)
