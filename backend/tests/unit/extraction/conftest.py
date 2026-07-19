from __future__ import annotations

import pytest
from _factories import make_document

from katilim_analiz.contracts import CleanDocument


@pytest.fixture
def campaign_document() -> CleanDocument:
    return make_document(
        ("heading", "Avantajlı Konut Finansmanı"),
        (
            "paragraph",
            "Bireysel müşterilere özel finansman kâr payı oranı aylık %1,89 olarak uygulanır.",
        ),
        ("paragraph", "500.000 TL finansman tutarı için 120 aya varan vade sunulur."),
        (
            "paragraph",
            "Kampanya 1 Temmuz 2026 - 31 Ağustos 2026 tarihleri arasında geçerlidir.",
        ),
        ("paragraph", "Başvurular yalnızca mobil şubeden yapılabilir."),
    )
