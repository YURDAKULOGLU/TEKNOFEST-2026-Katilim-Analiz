import type {
  CampaignType,
  ComparisonDimension,
  CoverageStatus,
  EvidenceStatus,
  ProductFamily,
  RecordStatus,
  SalesChannel,
} from "../api/contracts";

const productFamilyLabels: Record<ProductFamily, string> = {
  financing: "Finansman",
  card: "Kart",
  investment: "Yatırım",
  participation_account: "Katılma hesabı",
  other: "Diğer",
  unknown: "Belirsiz",
};

const campaignTypeLabels: Record<CampaignType, string> = {
  financing_rate: "Finansman oranı",
  profit_share: "Kâr payı dağıtımı",
  installment: "Taksit",
  cashback: "Nakit iade",
  discount: "İndirim",
  points: "Puan",
  fee_waiver: "Ücret muafiyeti",
  welcome: "Hoş geldin",
  other: "Diğer",
  unknown: "Belirsiz",
};

const salesChannelLabels: Record<SalesChannel, string> = {
  all: "Tüm kanallar",
  digital: "Dijital",
  mobile: "Mobil",
  web: "Web",
  branch: "Şube",
  unknown: "Belirsiz",
};

const comparisonDimensionLabels: Record<ComparisonDimension, string> = {
  rate: "Oran",
  term: "Vade",
  fee: "Ücret",
  reward: "Fayda",
  eligibility: "Uygunluk",
};

const coverageStatusLabels: Record<CoverageStatus, string> = {
  success: "Başarılı",
  not_found: "İçerik bulunamadı",
  unreachable: "Erişilemedi",
  blocked: "Erişim engellendi",
  partial: "Kısmi",
};

const evidenceStatusLabels: Record<EvidenceStatus, string> = {
  stated: "Kaynakta açıkça belirtilmiş",
  inferred: "Kanıttan çıkarılmış",
  unknown: "Bilinmiyor",
  ambiguous: "Belirsiz",
};

const recordStatusLabels: Record<RecordStatus, string> = {
  validated: "Doğrulanmış",
  needs_review: "İnceleme gerekli",
  rejected: "Reddedilmiş",
};

const reasonCodeLabels: Record<string, string> = {
  comparable: "Aynı bağlamda karşılaştırılabilir",
  product_family_mismatch: "Ürün aileleri farklı",
  rate_kind_mismatch: "Oran türleri farklı",
  rate_period_mismatch: "Oran dönemleri farklı",
  currency_mismatch: "Para birimleri farklı",
  eligibility_mismatch: "Müşteri uygunluk bağlamları farklı",
  validity_mismatch: "Geçerlilik dönemleri örtüşmüyor",
  missing_value: "Karşılaştırma için değer eksik",
  unknown_context: "Karşılaştırma bağlamı yeterince açık değil",
  record_not_validated: "Kayıt için insan incelemesi tamamlanmadı",
};

export function productFamilyLabel(value: ProductFamily): string {
  return productFamilyLabels[value];
}

export function campaignTypeLabel(value: CampaignType): string {
  return campaignTypeLabels[value];
}

export function salesChannelLabel(value: SalesChannel): string {
  return salesChannelLabels[value];
}

export function comparisonDimensionLabel(value: ComparisonDimension): string {
  return comparisonDimensionLabels[value];
}

export function coverageStatusLabel(value: CoverageStatus): string {
  return coverageStatusLabels[value];
}

export function evidenceStatusLabel(value: EvidenceStatus): string {
  return evidenceStatusLabels[value];
}

export function recordStatusLabel(value: RecordStatus): string {
  return recordStatusLabels[value];
}

export function reasonCodeLabel(value: string): string {
  return reasonCodeLabels[value] ?? value.replaceAll("_", " ");
}
