from datetime import UTC, datetime
from decimal import Decimal

import pytest

from katilim_analiz.storage.serialization import canonical_json, canonical_sha256, json_value


def test_canonical_json_is_order_independent_and_preserves_decimal_precision() -> None:
    first = {"amount": Decimal("100000.1234"), "at": datetime(2026, 7, 18, tzinfo=UTC)}
    second = {"at": datetime(2026, 7, 18, tzinfo=UTC), "amount": Decimal("100000.1234")}

    assert canonical_json(first) == canonical_json(second)
    assert canonical_sha256(first) == canonical_sha256(second)
    assert '"amount":"100000.1234"' in canonical_json(first)


def test_json_value_rejects_implicit_stringification_of_unknown_objects() -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        json_value(object())


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        canonical_json({"score": float("nan")})
