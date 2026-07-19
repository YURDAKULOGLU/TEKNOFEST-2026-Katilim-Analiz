"""Locale-specific API presentation without changing canonical contract values."""

from __future__ import annotations

from collections.abc import Mapping

from katilim_analiz.application.models import CampaignFacets, CampaignListResponse, FacetOption

_PRODUCT_FAMILY_LABELS: Mapping[str, str] = {
    "financing": "Finansman",
    "card": "Kart",
    "investment": "Yatırım",
    "participation_account": "Katılma Hesabı",
    "other": "Diğer",
    "unknown": "Bilinmiyor",
}

_SALES_CHANNEL_LABELS: Mapping[str, str] = {
    "all": "Tüm Kanallar",
    "digital": "Dijital",
    "mobile": "Mobil",
    "web": "İnternet",
    "branch": "Şube",
    "unknown": "Bilinmiyor",
}


def _translated(options: list[FacetOption], labels: Mapping[str, str]) -> list[FacetOption]:
    return [
        option.model_copy(update={"label": labels.get(option.value, option.label)})
        for option in options
    ]


def localize_campaign_list(response: CampaignListResponse) -> CampaignListResponse:
    """Translate display labels while preserving filter values, counts, and ordering."""

    facets = response.facets
    localized = CampaignFacets(
        banks=facets.banks,
        product_families=_translated(facets.product_families, _PRODUCT_FAMILY_LABELS),
        currencies=facets.currencies,
        customer_segments=facets.customer_segments,
        sales_channels=_translated(facets.sales_channels, _SALES_CHANNEL_LABELS),
    )
    return response.model_copy(update={"facets": localized})
