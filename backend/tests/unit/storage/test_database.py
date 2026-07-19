import pytest

from katilim_analiz.storage.database import DatabaseConfigurationError, validated_asyncpg_url


def test_asyncpg_postgresql_url_is_required() -> None:
    url = validated_asyncpg_url("postgresql+asyncpg://user:secret@db.internal/app")

    assert url.drivername == "postgresql+asyncpg"
    assert url.database == "app"


@pytest.mark.parametrize(
    "value",
    [
        "sqlite+aiosqlite:///app.db",
        "postgresql://user:secret@localhost/app",
        "postgresql+asyncpg://user:secret@localhost",
    ],
)
def test_non_async_postgresql_urls_are_rejected(value: str) -> None:
    with pytest.raises(DatabaseConfigurationError):
        validated_asyncpg_url(value)
