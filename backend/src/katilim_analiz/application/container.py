"""Explicit application composition passed into the HTTP boundary."""

from dataclasses import dataclass

from katilim_analiz.application.health import ReadinessService
from katilim_analiz.application.preview import ExtractionPreviewService
from katilim_analiz.application.services import CampaignService, ChatService


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    campaigns: CampaignService
    chat: ChatService
    readiness: ReadinessService
    previews: ExtractionPreviewService
