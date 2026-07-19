import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiProblem, createApiClient } from "../src/api/client";

describe("typed API client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("encodes active campaign filters, cursor, and the exact snapshot", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({ items: [], next_cursor: null, facets: {}, as_of: "2026-07-18T12:00:00Z" }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = createApiClient({ baseUrl: "http://localhost:8080/api/v1" });
    await client.listCampaigns(
      { bank_id: "bank-a", currency: "TRY", validity: "active" },
      "next 1",
      "2026-07-18T12:00:00+03:00",
    );

    expect(fetchMock).toHaveBeenCalledOnce();
    const request = fetchMock.mock.calls[0]?.[0];
    expect(String(request)).toBe(
      "http://localhost:8080/api/v1/campaigns?bank_id=bank-a&currency=TRY&validity=active&cursor=next+1&as_of=2026-07-18T12%3A00%3A00%2B03%3A00",
    );
  });

  it("surfaces RFC 9457 problem details with the correlation id", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(
          JSON.stringify({
            type: "https://example.test/problems/unavailable",
            title: "Hizmet kullanılamıyor",
            status: 503,
            detail: "Veritabanına ulaşılamadı.",
            code: "database_unavailable",
            correlation_id: "corr-test-1",
            errors: [],
          }),
          { status: 503, headers: { "content-type": "application/problem+json" } },
        ),
      ),
    );

    const client = createApiClient({ baseUrl: "/api/v1" });
    const error = await client.getCoverage().catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiProblem);
    expect(error).toMatchObject({
      status: 503,
      code: "database_unavailable",
      correlationId: "corr-test-1",
      message: "Veritabanına ulaşılamadı.",
    });
  });

  it("sends the canonical grounded-chat request contract", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          answer: "Kanıt bulunamadı.",
          plan: {
            intent: "unknown",
            bank_ids: [],
            product_family: null,
            campaign_type: null,
            comparison_dimensions: [],
            keywords: [],
            limit: 5,
          },
          citations: [],
          insufficient_evidence: true,
          warnings: [],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = createApiClient({ baseUrl: "/api/v1" });
    await client.sendChatMessage({
      question: "Güncel kampanyalar hangileri?",
      as_of: "2026-07-18T12:00:00+03:00",
    });

    const requestInit = fetchMock.mock.calls[0]?.[1];
    expect(requestInit?.method).toBe("POST");
    expect(requestInit?.body).toBe(
      JSON.stringify({
        question: "Güncel kampanyalar hangileri?",
        as_of: "2026-07-18T12:00:00+03:00",
      }),
    );
  });

  it("sends only bank_id and text to the non-persistent preview endpoint", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          scope: "unverified_preview",
          human_verified: false,
          persisted: false,
          status: "abstained",
          input_sha256: "a".repeat(64),
          candidate: null,
          issues: ["extraction_abstained"],
          model_attempted: false,
          accepted_model_facts: 0,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = createApiClient({ baseUrl: "/api/v1" });
    await client.previewExtraction({
      bank_id: "adil-katilim",
      text: "Konut Finansmanı Kampanyası\n12 ay vadeli.",
    });

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "/api/v1/previews/extractions",
    );
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        bank_id: "adil-katilim",
        text: "Konut Finansmanı Kampanyası\n12 ay vadeli.",
      }),
    });
  });

  it("polls the read-only notification feed with an opaque cursor", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [], next_cursor: "next-2" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = createApiClient({ baseUrl: "/api/v1" });
    await client.listNotifications("next 1", 100);

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "/api/v1/notifications?cursor=next+1&limit=100",
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBeUndefined();
  });
});
