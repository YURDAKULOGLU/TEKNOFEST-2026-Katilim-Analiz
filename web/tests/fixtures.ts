import type {
  CampaignDetail,
  CampaignListResponse,
  ChatAnswer,
  ComparisonResponse,
  CoverageEntry,
  ExtractionPreviewResponse,
  NotificationListResponse,
} from "../src/api/contracts";

export const campaignList: CampaignListResponse = {
  items: [
    {
      id: "campaign-a",
      campaign_key: "test-bank-a:campaign-a",
      version: 1,
      bank_id: "test-bank-a",
      bank_name: "Test Katılım A",
      title: "Test Konut Finansmanı",
      product_family: "financing",
      campaign_type: "financing_rate",
      summary: "Yalnızca arayüz testi için oluşturulmuş kayıt.",
      currency: "TRY",
      customer_segments: ["Bireysel"],
      sales_channel: "branch",
      validity: {
        raw: "1-31 Temmuz 2026",
        starts_on: "2026-07-01",
        ends_on: "2026-07-31",
        status: "stated",
      },
      observed_at: "2026-07-18T12:00:00+03:00",
      status: "validated",
      evidence_count: 2,
      primary_value: { label: "Aylık kâr oranı", value: "%2,49" },
    },
    {
      id: "campaign-b",
      campaign_key: "test-bank-b:campaign-b",
      version: 2,
      bank_id: "test-bank-b",
      bank_name: "Test Katılım B",
      title: "Test Kart İndirimi",
      product_family: "card",
      campaign_type: "discount",
      summary: null,
      currency: "TRY",
      customer_segments: ["Tüm müşteriler"],
      sales_channel: "mobile",
      validity: null,
      observed_at: "2026-07-17T10:30:00+03:00",
      status: "needs_review",
      evidence_count: 1,
      primary_value: { label: "İndirim", value: "%10" },
    },
  ],
  next_cursor: null,
  facets: {
    banks: [
      { value: "test-bank-a", label: "Test Katılım A", count: 1 },
      { value: "test-bank-b", label: "Test Katılım B", count: 1 },
    ],
    product_families: [
      { value: "financing", label: "Finansman", count: 1 },
      { value: "card", label: "Kart", count: 1 },
    ],
    currencies: [{ value: "TRY", label: "TRY", count: 2 }],
    customer_segments: [{ value: "bireysel", label: "Bireysel", count: 1 }],
    sales_channels: [{ value: "mobile", label: "Mobil", count: 1 }],
  },
  as_of: "2026-07-18T12:15:00+03:00",
};

export const emptyNotificationPage: NotificationListResponse = {
  items: [],
  next_cursor: null,
};

export const campaignDetail: CampaignDetail = {
  campaign: campaignList.items[0]!,
  source_document_id: "document-a",
  source_url: "https://example.test/kampanya-a",
  source_title: "Test kaynak sayfası",
  record_sha256: "a".repeat(64),
  extraction: {
    method: "hybrid",
    extractor_version: "test-1",
    schema_version: "1.0.0",
    model_id: "test-model",
  },
  evidence: [
    {
      id: "evidence-a",
      field_pointer: "/rates/0/value_percent",
      quote: "Aylık kâr oranı %2,49",
      status: "stated",
      block_id: "block-a",
    },
  ],
  validation_issues: [],
};

export const coverage: CoverageEntry[] = [
  {
    bank_id: "test-bank-a",
    bank_name: "Test Katılım A",
    observed_at: "2026-07-18T12:00:00+03:00",
    status: "success",
    source_count: 1,
    campaign_count: 1,
    reason: null,
  },
  {
    bank_id: "test-bank-c",
    bank_name: "Test Katılım C",
    observed_at: "2026-07-18T11:00:00+03:00",
    status: "unreachable",
    source_count: 0,
    campaign_count: 0,
    reason: "Test ortamında kaynak erişilemedi.",
  },
];

export const incompatibleComparison: ComparisonResponse = {
  ruleset_version: "test-rules-1",
  generated_at: "2026-07-18T12:20:00+03:00",
  items: [
    {
      campaign_id: "campaign-a",
      dimension: "rate",
      rank: null,
      display_value: "%2,49 (aylık finansman kâr oranı)",
      comparable: false,
      reason_code: "product_family_mismatch",
      evidence_ids: ["evidence-a"],
    },
    {
      campaign_id: "campaign-b",
      dimension: "rate",
      rank: null,
      display_value: "%10 (indirim)",
      comparable: false,
      reason_code: "product_family_mismatch",
      evidence_ids: ["evidence-b"],
    },
  ],
  warnings: ["Ürün aileleri farklı olduğu için oranlar sıralanmadı."],
  canonical_sha256: "b".repeat(64),
};

export const abstainingAnswer: ChatAnswer = {
  answer: "Bu soruyu yanıtlamak için doğrulanmış kaynak kanıtı bulunamadı.",
  plan: {
    intent: "unknown",
    bank_ids: [],
    product_family: null,
    campaign_type: null,
    comparison_dimensions: [],
    keywords: ["test"],
    limit: 5,
  },
  citations: [],
  insufficient_evidence: true,
  warnings: ["Yanıt üretilmedi; kaynak kapsamını daraltmayı deneyin."],
};

export const extractionPreview: ExtractionPreviewResponse = {
  scope: "unverified_preview",
  human_verified: false,
  persisted: false,
  status: "needs_review",
  input_sha256: "c".repeat(64),
  candidate: {
    id: "candidate:preview-test",
    source_document_id: "preview-document:test",
    data: {
      bank_id: "test-bank-a",
      title: "Test Metin Kampanyası",
      product_family: "financing",
      campaign_type: "financing_rate",
      summary: "Bireysel müşterilere özel test finansmanı.",
      rates: [
        {
          raw: "Aylık finansman kâr payı oranı %1,99",
          value_percent: "1.99",
          kind: "financing_profit_rate",
          period: "monthly",
          gross_net_basis: "unspecified",
          reference_starts_on: null,
          reference_ends_on: null,
          term_months: 12,
          basis_label: null,
          status: "stated",
        },
      ],
      financing_amounts: [
        {
          raw: "100.000 TL",
          amount: "100000",
          currency: "TRY",
          status: "stated",
        },
      ],
      terms: [
        {
          raw: "12 ay vadeli",
          minimum_months: null,
          maximum_months: 12,
          status: "stated",
        },
      ],
      fees: [
        {
          raw: "250 TL tahsis ücreti",
          money: {
            raw: "250 TL",
            amount: "250",
            currency: "TRY",
            status: "stated",
          },
          rate: null,
          kind: "allocation",
          basis: "one_time",
          description: null,
          status: "stated",
        },
      ],
      rewards: [
        {
          raw: "1.000 puan",
          kind: "points",
          basis: "campaign_total",
          program_name: null,
          money: null,
          rate: null,
          points: "1000",
          minimum_spend: null,
          maximum_money: null,
          maximum_points: null,
          description: null,
          status: "stated",
        },
      ],
      validity: {
        raw: "1-31 Temmuz 2026",
        starts_on: "2026-07-01",
        ends_on: "2026-07-31",
        status: "stated",
      },
      customer_segments: ["Bireysel"],
      eligibility_conditions: ["Mobil başvuru gereklidir."],
      comparison_context: {
        product_currency: "TRY",
        customer_segment_keys: ["bireysel"],
        sales_channel: "mobile",
        new_customer_only: false,
        product_mechanism: "konut_finansmani",
        secured: true,
      },
    },
    evidence: [
      {
        id: "evidence:preview-title",
        field_pointer: "/data/title",
        source_document_id: "preview-document:test",
        block_id: "preview-block:0:test",
        quote: "Test Metin Kampanyası",
        start_char: 0,
        end_char: 21,
        evidence_sha256: "d".repeat(64),
        status: "stated",
      },
    ],
    metadata: {
      method: "rule",
      extractor_version: "test-preview/1.0",
      schema_version: "campaign-candidate/1.0",
      prompt_version: null,
      model_id: null,
      model_digest: null,
      started_at: "2026-07-18T12:00:00+03:00",
      completed_at: "2026-07-18T12:00:00+03:00",
    },
    issues: ["unresolved:product_family"],
  },
  issues: ["unresolved:product_family"],
  model_attempted: false,
  accepted_model_facts: 0,
};
