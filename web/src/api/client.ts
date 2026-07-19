import type {
  CampaignDetail,
  CampaignFilters,
  CampaignListResponse,
  ChatAnswer,
  ChatRequest,
  ComparisonRequest,
  ComparisonResponse,
  CoverageResponse,
  ExtractionPreviewRequest,
  ExtractionPreviewResponse,
  NotificationListResponse,
  ApiContractPath,
  ProblemDetail,
} from "./contracts";
import { getApiBaseUrl } from "./runtime-config";

interface ApiClientOptions {
  baseUrl?: string;
}

const CONTRACT_PATHS = {
  campaigns: "/api/v1/campaigns",
  campaignDetail: "/api/v1/campaigns/{campaign_id}",
  coverage: "/api/v1/coverage",
  comparisons: "/api/v1/comparisons",
  chat: "/api/v1/chat",
  extractionPreview: "/api/v1/previews/extractions",
  notifications: "/api/v1/notifications",
} as const satisfies Record<string, ApiContractPath>;

const CONTRACT_PREFIX = "/api/v1";

function relativeEndpoint(path: ApiContractPath): string {
  if (!path.startsWith(`${CONTRACT_PREFIX}/`)) {
    throw new Error(`API contract path is outside ${CONTRACT_PREFIX}: ${path}`);
  }
  return path.slice(CONTRACT_PREFIX.length);
}

export interface AnalysisApi {
  listCampaigns(
    filters: CampaignFilters,
    cursor?: string,
    asOf?: string,
  ): Promise<CampaignListResponse>;
  getCampaign(id: string): Promise<CampaignDetail>;
  getCoverage(): Promise<CoverageResponse>;
  compareCampaigns(request: ComparisonRequest): Promise<ComparisonResponse>;
  sendChatMessage(request: ChatRequest): Promise<ChatAnswer>;
  previewExtraction(request: ExtractionPreviewRequest): Promise<ExtractionPreviewResponse>;
  listNotifications(cursor?: string, limit?: number): Promise<NotificationListResponse>;
}

export class ApiProblem extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | null;
  readonly type: string;
  readonly validationErrors: readonly Record<string, unknown>[];

  constructor(problem: ProblemDetail) {
    super(problem.detail || problem.title);
    this.name = "ApiProblem";
    this.status = problem.status;
    this.code = problem.code;
    this.correlationId = problem.correlation_id;
    this.type = problem.type;
    this.validationErrors = problem.errors;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function asProblemDetail(payload: unknown, status: number): ProblemDetail {
  if (!isRecord(payload)) {
    return {
      type: "about:blank",
      title: "İstek tamamlanamadı",
      status,
      detail: "Sunucudan beklenmeyen bir hata yanıtı alındı.",
      instance: null,
      code: "unexpected_error_response",
      correlation_id: null,
      errors: [],
    };
  }

  return {
    type: typeof payload.type === "string" ? payload.type : "about:blank",
    title: typeof payload.title === "string" ? payload.title : "İstek tamamlanamadı",
    status: typeof payload.status === "number" ? payload.status : status,
    detail: typeof payload.detail === "string" ? payload.detail : null,
    instance: typeof payload.instance === "string" ? payload.instance : null,
    code: typeof payload.code === "string" ? payload.code : "request_failed",
    correlation_id: typeof payload.correlation_id === "string" ? payload.correlation_id : null,
    errors: Array.isArray(payload.errors)
      ? payload.errors.filter((entry): entry is Record<string, unknown> => isRecord(entry))
      : [],
  };
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

function queryString(filters: CampaignFilters, cursor?: string, asOf?: string): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) {
      params.set(key, value);
    }
  }
  if (cursor) {
    params.set("cursor", cursor);
  }
  if (asOf) {
    params.set("as_of", asOf);
  }
  const value = params.toString();
  return value ? `?${value}` : "";
}

function notificationQueryString(cursor?: string, limit?: number): string {
  const params = new URLSearchParams();
  if (cursor) {
    params.set("cursor", cursor);
  }
  if (limit !== undefined) {
    params.set("limit", String(limit));
  }
  const value = params.toString();
  return value ? `?${value}` : "";
}

export function createApiClient(options: ApiClientOptions = {}): AnalysisApi {
  const baseUrl = (options.baseUrl || getApiBaseUrl()).replace(/\/$/, "");

  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${baseUrl}${path}`, {
      ...init,
      credentials: "same-origin",
      headers: {
        accept: "application/json, application/problem+json",
        ...(init?.body ? { "content-type": "application/json" } : {}),
        ...init?.headers,
      },
    });
    const payload = await readJson(response);
    if (!response.ok) {
      throw new ApiProblem(asProblemDetail(payload, response.status));
    }
    return payload as T;
  }

  return {
    listCampaigns(filters, cursor, asOf) {
      return request<CampaignListResponse>(
        `${relativeEndpoint(CONTRACT_PATHS.campaigns)}${queryString(filters, cursor, asOf)}`,
      );
    },
    getCampaign(id) {
      return request<CampaignDetail>(
        relativeEndpoint(CONTRACT_PATHS.campaignDetail).replace(
          "{campaign_id}",
          encodeURIComponent(id),
        ),
      );
    },
    getCoverage() {
      return request<CoverageResponse>(relativeEndpoint(CONTRACT_PATHS.coverage));
    },
    compareCampaigns(comparisonRequest) {
      return request<ComparisonResponse>(relativeEndpoint(CONTRACT_PATHS.comparisons), {
        method: "POST",
        body: JSON.stringify(comparisonRequest),
      });
    },
    sendChatMessage(chatRequest) {
      return request<ChatAnswer>(relativeEndpoint(CONTRACT_PATHS.chat), {
        method: "POST",
        body: JSON.stringify(chatRequest),
      });
    },
    previewExtraction(previewRequest) {
      return request<ExtractionPreviewResponse>(
        relativeEndpoint(CONTRACT_PATHS.extractionPreview),
        {
          method: "POST",
          body: JSON.stringify(previewRequest),
        },
      );
    },
    listNotifications(cursor, limit) {
      return request<NotificationListResponse>(
        `${relativeEndpoint(CONTRACT_PATHS.notifications)}${notificationQueryString(cursor, limit)}`,
      );
    },
  };
}
