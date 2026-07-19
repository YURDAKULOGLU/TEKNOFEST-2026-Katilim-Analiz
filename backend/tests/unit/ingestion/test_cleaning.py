from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from katilim_analiz.contracts import FetchArtifact, FetchStatus
from katilim_analiz.ingestion.cleaning import CleaningError, clean_html

RAW_HTML = """<!doctype html>
<html lang="tr">
  <head>
    <title>  Yaz F\u0131rsat\u0131  </title>
    <script>ignoreThis()</script>
    <style>.hidden { display: none }</style>
  </head>
  <body>
    <nav>Menü metni</nav>
    <main>
      <h1>Yaz\u00a0F\u0131rsat\u0131</h1>
      <p>10.000 TL\u2019ye varan   finansman.</p>
      <ul><li>Son başvuru: 31 Temmuz 2026</li></ul>
      <table>
        <tr><th>Vade</th><th>Oran</th></tr>
        <tr><td>12 ay</td><td>%2,49</td></tr>
      </table>
      <form><label>Şifre</label><input name="password"></form>
    </main>
  </body>
</html>
""".encode()


def _artifact(raw: bytes = RAW_HTML) -> FetchArtifact:
    digest = hashlib.sha256(raw).hexdigest()
    return FetchArtifact(
        id=f"fetch:{digest}",
        bank_id="kuveyt-turk",
        requested_url="https://www.kuveytturk.com.tr/kampanya",
        final_url="https://www.kuveytturk.com.tr/kampanya",
        status=FetchStatus.SUCCESS,
        http_status=200,
        fetched_at=datetime(2026, 7, 18, 10, tzinfo=UTC),
        robots_allowed=True,
        content_type="text/html; charset=utf-8",
        raw_sha256=digest,
        raw_size_bytes=len(raw),
        private_raw_path=f"sha256/{digest[:2]}/{digest}.html",
    )


def test_cleaner_produces_ordered_evidence_addressable_blocks() -> None:
    document = clean_html(
        _artifact(),
        RAW_HTML,
        cleaned_at=datetime(2026, 7, 18, 10, 1, tzinfo=UTC),
    )

    assert document.title == "Yaz F\u0131rsat\u0131"
    assert [block.kind for block in document.blocks] == [
        "heading",
        "paragraph",
        "list_item",
        "table",
        "table",
    ]
    assert [block.text for block in document.blocks] == [
        "Yaz F\u0131rsat\u0131",
        "10.000 TL\u2019ye varan finansman.",
        "Son başvuru: 31 Temmuz 2026",
        "Vade | Oran",
        "12 ay | %2,49",
    ]
    assert [block.ordinal for block in document.blocks] == list(range(5))
    assert len({block.id for block in document.blocks}) == 5
    assert all(block.locator.startswith("html") for block in document.blocks)
    assert all(
        block.text_sha256 == hashlib.sha256(block.text.encode()).hexdigest()
        for block in document.blocks
    )
    assert "ignoreThis" not in " ".join(block.text for block in document.blocks)
    assert "Şifre" not in " ".join(block.text for block in document.blocks)
    assert "Menü" not in " ".join(block.text for block in document.blocks)


def test_cleaner_is_deterministic_for_equivalent_whitespace() -> None:
    first = clean_html(_artifact(), RAW_HTML, cleaned_at=datetime(2026, 7, 18, 10, 1, tzinfo=UTC))
    changed = RAW_HTML.replace(b"   finansman", b" finansman")
    second = clean_html(
        _artifact(changed),
        changed,
        cleaned_at=datetime(2026, 7, 18, 10, 2, tzinfo=UTC),
    )

    assert first.clean_sha256 == second.clean_sha256
    assert [block.id for block in first.blocks] == [block.id for block in second.blocks]
    assert first.id == second.id == f"clean:{first.clean_sha256}"


def test_cleaner_rejects_hash_mismatch_non_success_and_empty_content() -> None:
    with pytest.raises(CleaningError, match="hash"):
        clean_html(_artifact(), b"<p>different</p>")

    failed = _artifact().model_copy(update={"status": FetchStatus.FAILED})
    with pytest.raises(CleaningError, match="successful"):
        clean_html(failed, RAW_HTML)

    empty = b"<html><script>only hidden content</script></html>"
    with pytest.raises(CleaningError, match="no evidence blocks"):
        clean_html(_artifact(empty), empty)


def test_cleaner_splits_oversized_text_without_losing_order() -> None:
    raw = ("<main><p>" + "a" * 50_010 + "</p><p>son</p></main>").encode()
    document = clean_html(_artifact(raw), raw)

    assert [len(block.text) for block in document.blocks] == [50_000, 10, 3]
    assert [block.ordinal for block in document.blocks] == [0, 1, 2]


def test_cleaner_keeps_leaf_layout_text_and_removes_hidden_content() -> None:
    raw = b"""
    <html><body><main>
      <div class="wrapper">
        <div class="campaign-copy"><span>Leaf layout campaign text</span></div>
        <div hidden>hidden attribute</div>
        <div aria-hidden="true">aria hidden</div>
        <div style="display: none">CSS hidden</div>
      </div>
    </main></body></html>
    """

    document = clean_html(_artifact(raw), raw)

    assert [(block.kind, block.text) for block in document.blocks] == [
        ("other", "Leaf layout campaign text")
    ]


def test_cleaner_removes_observed_cookie_and_category_navigation_containers_only() -> None:
    raw = """
    <html><body><main>
      <div id="cookie-dialog-content" class="d-none">
        <div class="switch-desc"><strong>Zorunlu Çerezler</strong></div>
      </div>
      <div class="bankkart-category-list">
        <a href="/kart-kampanyalari"><div>Tüm Kampanyalar</div></a>
        <a href="/kart-kampanyalari/mobilya"><div>Mobilya ve Dekorasyon</div></a>
      </div>
      <h1>Puffy'de 6'ya varan Taksit</h1>
      <p>Ücretsiz ve ticari kredi kartlarımız kampanyaya dahil değildir.</p>
      <p>Bireysel kartlar için azami taksit sayısı değişebilir.</p>
      <p>Kampanya metni zorunlu çerezler hakkında bilgi verebilir.</p>
    </main></body></html>
    """.encode()

    document = clean_html(_artifact(raw), raw)
    texts = [block.text for block in document.blocks]

    assert texts == [
        "Puffy'de 6'ya varan Taksit",
        "Ücretsiz ve ticari kredi kartlarımız kampanyaya dahil değildir.",
        "Bireysel kartlar için azami taksit sayısı değişebilir.",
        "Kampanya metni zorunlu çerezler hakkında bilgi verebilir.",
    ]
