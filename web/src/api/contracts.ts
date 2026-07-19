/** Public UI types projected from the generated FastAPI OpenAPI contract. */

import type { components, paths } from "./openapi.generated";

type Schemas = components["schemas"];
type CampaignListOperation = paths["/api/v1/campaigns"]["get"];
type CampaignDetailOperation = paths["/api/v1/campaigns/{campaign_id}"]["get"];
type CoverageOperation = paths["/api/v1/coverage"]["get"];
type ComparisonOperation = paths["/api/v1/comparisons"]["post"];
type ChatOperation = paths["/api/v1/chat"]["post"];
type ExtractionPreviewOperation = paths["/api/v1/previews/extractions"]["post"];
type NotificationListOperation = paths["/api/v1/notifications"]["get"];

export type ApiContractPath = keyof paths;
export type CoverageStatus = Schemas["CoverageStatus"];
export type ProductFamily = Schemas["ProductFamily"];
export type CampaignType = Schemas["CampaignType"];
export type SalesChannel = Schemas["SalesChannel"];
export type EvidenceStatus = Schemas["EvidenceStatus"];
export type RecordStatus = Schemas["RecordStatus"];
export type ComparisonDimension = Schemas["ComparisonDimension"];
export type CitationStatus = Schemas["CitationStatus"];
export type QueryIntent = Schemas["QueryIntent"];
export type ValidityFilter = Schemas["ValidityFilter"];

export type ValidityWindow = Schemas["ValidityWindow"];
export type PrimaryValue = Schemas["PrimaryValue"];
export type CampaignSummary = Schemas["CampaignSummary"];
export type CampaignChangeEvent = Schemas["CampaignChangeEvent"];
export type FacetOption = Schemas["FacetOption"];
export type CampaignFacets = Schemas["CampaignFacets"];
export type EvidenceProjection = Schemas["EvidenceProjection"];
export type EvidenceRef = Schemas["EvidenceRef"];
export type ExtractionProjection = Schemas["ExtractionProjection"];
export type ExtractionCandidate = Schemas["ExtractionCandidate"];
export type ComparisonItem = Schemas["ComparisonItem"];
export type ChatQueryPlan = Schemas["ChatQueryPlan"];
export type AnswerCitation = Schemas["AnswerCitation"];
export type ProblemDetail = Schemas["ProblemDetail"];

export type CampaignListResponse =
  CampaignListOperation["responses"][200]["content"]["application/json"];
export type CampaignDetail =
  CampaignDetailOperation["responses"][200]["content"]["application/json"];
export type CoverageResponse =
  CoverageOperation["responses"][200]["content"]["application/json"];
export type CoverageEntry = CoverageResponse[number];
export type ComparisonRequest =
  ComparisonOperation["requestBody"]["content"]["application/json"];
export type ComparisonResponse =
  ComparisonOperation["responses"][200]["content"]["application/json"];
export type ChatRequest = ChatOperation["requestBody"]["content"]["application/json"];
export type ChatAnswer = ChatOperation["responses"][200]["content"]["application/json"];
export type ExtractionPreviewRequest =
  ExtractionPreviewOperation["requestBody"]["content"]["application/json"];
export type ExtractionPreviewResponse =
  ExtractionPreviewOperation["responses"][200]["content"]["application/json"];
export type NotificationListResponse =
  NotificationListOperation["responses"][200]["content"]["application/json"];

type CampaignQuery = NonNullable<CampaignListOperation["parameters"]["query"]>;
export type CampaignFilters = Omit<CampaignQuery, "as_of" | "cursor" | "limit">;
