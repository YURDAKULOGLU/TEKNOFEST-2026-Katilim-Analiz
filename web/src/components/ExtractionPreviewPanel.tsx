import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import type { AnalysisApi } from "../api/client";
import type { CoverageEntry, ExtractionPreviewResponse } from "../api/contracts";
import {
  campaignTypeLabel,
  evidenceStatusLabel,
  productFamilyLabel,
} from "../utils/labels";
import { ErrorState } from "./AsyncState";

interface ExtractionPreviewPanelProps {
  client: AnalysisApi;
  banks: readonly CoverageEntry[] | undefined;
  banksLoading: boolean;
}

function joined(values: readonly string[]): string {
  return values.length > 0 ? values.join(" · ") : "Bulunamadı";
}

function StructuredPreview({ result }: { result: ExtractionPreviewResponse }) {
  const candidate = result.candidate;
  const structuredData = candidate ? JSON.stringify(candidate.data, null, 2) : null;
  return (
    <article className="comparison-result" aria-live="polite">
      <div className="result-heading">
        <div>
          <p className="eyebrow">Doğrulanmamış çıkarım önizlemesi</p>
          <h3>{candidate ? candidate.data.title : "Yeterli yapılandırılmış kanıt bulunamadı"}</h3>
        </div>
        <span className={`status-badge status-badge--${result.status}`}>
          {result.status === "needs_review" ? "İnsan incelemesi gerekli" : "Çekimser kalındı"}
        </span>
      </div>

      <p className="inline-warning">
        Bu çıktı insan tarafından doğrulanmadı ve kaydedilmedi. Kampanya listesine,
        karşılaştırmalara veya kaynaklı asistan verisine girmez.
      </p>
      <p className="inline-warning">
        Seçilen banka kullanıcı tarafından beyan edilen bir atıftır; bu önizleme metnin
        kaynağını veya bankaya aitliğini doğrulamaz.
      </p>

      {candidate ? (
        <>
          <dl className="detail-summary">
            <div>
              <span>Ürün ailesi</span>
              <strong>{productFamilyLabel(candidate.data.product_family)}</strong>
            </div>
            <div>
              <span>Kampanya türü</span>
              <strong>{campaignTypeLabel(candidate.data.campaign_type)}</strong>
            </div>
            <div>
              <span>Oranlar</span>
              <strong>{joined(candidate.data.rates.map((rate) => rate.raw))}</strong>
            </div>
            <div>
              <span>Tutarlar</span>
              <strong>{joined(candidate.data.financing_amounts.map((money) => money.raw))}</strong>
            </div>
            <div>
              <span>Vadeler</span>
              <strong>{joined(candidate.data.terms.map((term) => term.raw))}</strong>
            </div>
            <div>
              <span>Müşteri segmentleri</span>
              <strong>{joined(candidate.data.customer_segments)}</strong>
            </div>
            <div>
              <span>Koşullar</span>
              <strong>{joined(candidate.data.eligibility_conditions)}</strong>
            </div>
            <div>
              <span>Model katkısı</span>
              <strong>
                {result.model_attempted
                  ? `${result.accepted_model_facts} kanıta bağlı alan kabul edildi`
                  : "Model çağrılmadı"}
              </strong>
            </div>
          </dl>

          <section aria-labelledby="preview-structured-data-heading">
            <div className="section-heading section-heading--compact">
              <h4 id="preview-structured-data-heading">Tam normalize kampanya verisi</h4>
              <p>Salt okunur · eksiksiz alan görünümü</p>
            </div>
            <textarea
              aria-label="Tam normalize kampanya verisi"
              readOnly
              rows={24}
              spellCheck={false}
              value={structuredData ?? ""}
            />
          </section>

          <section aria-labelledby="preview-evidence-heading">
            <div className="section-heading section-heading--compact">
              <h4 id="preview-evidence-heading">Alan düzeyinde kanıt</h4>
              <p>{candidate.evidence.length} alıntı</p>
            </div>
            <ol className="evidence-list">
              {candidate.evidence.map((evidence) => (
                <li key={evidence.id}>
                  <div>
                    <code>{evidence.field_pointer}</code>
                    <span className={`status-badge status-badge--${evidence.status}`}>
                      {evidenceStatusLabel(evidence.status)}
                    </span>
                  </div>
                  <blockquote>{evidence.quote}</blockquote>
                  <small className="technical-reference">Blok: {evidence.block_id}</small>
                </li>
              ))}
            </ol>
          </section>
        </>
      ) : null}

      {result.issues.length > 0 ? (
        <details className="coverage-details">
          <summary>İnceleme notları ({result.issues.length})</summary>
          <ul>
            {result.issues.map((issue) => (
              <li key={issue}>
                <code>{issue}</code>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      <p className="technical-reference">Girdi SHA-256: {result.input_sha256}</p>
    </article>
  );
}

export function ExtractionPreviewPanel({
  client,
  banks,
  banksLoading,
}: ExtractionPreviewPanelProps) {
  const [bankId, setBankId] = useState("");
  const [text, setText] = useState("");
  const preview = useMutation({
    mutationFn: () => client.previewExtraction({ bank_id: bankId, text }),
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (bankId && text.trim()) {
      preview.mutate();
    }
  }

  return (
    <div className="chat-workspace">
      <form onSubmit={submit}>
        <label htmlFor="preview-bank">Önizleme bankası</label>
        <select
          id="preview-bank"
          required
          value={bankId}
          disabled={banksLoading || preview.isPending}
          onChange={(event) => {
            setBankId(event.target.value);
            preview.reset();
          }}
        >
          <option value="">{banksLoading ? "Banka kapsamı yükleniyor…" : "Banka seçin"}</option>
          {(banks ?? []).map((bank) => (
            <option key={bank.bank_id} value={bank.bank_id}>
              {bank.bank_name}
            </option>
          ))}
        </select>

        <label htmlFor="preview-text">Kampanya veya ürün metni</label>
        <textarea
          id="preview-text"
          required
          rows={9}
          maxLength={20_000}
          value={text}
          disabled={preview.isPending}
          placeholder={
            "İlk boş olmayan satıra kampanya başlığını, sonraki satırlara koşulları yapıştırın."
          }
          onChange={(event) => {
            setText(event.target.value);
            preview.reset();
          }}
        />
        <div className="chat-form-footer">
          <span>{text.length}/20.000 · URL veya dosya işlenmez</span>
          <button
            className="button button--primary"
            type="submit"
            disabled={!bankId || !text.trim() || preview.isPending}
          >
            {preview.isPending ? "Yerel çıkarım sürüyor…" : "Çıkarımı önizle"}
          </button>
        </div>
        <p className="technical-reference">
          Kural katmanı önce çalışır. Yerel model profili açıksa yalnız çözülemeyen alanlar için
          CPU üzerinde çağrı yapılır; ilk yanıt yaklaşık iki dakikaya kadar sürebilir.
        </p>
      </form>

      {preview.error ? (
        <ErrorState
          error={preview.error}
          onRetry={() => preview.mutate()}
          title="Çıkarım önizlemesi üretilemedi"
        />
      ) : null}
      {preview.data ? <StructuredPreview result={preview.data} /> : null}
    </div>
  );
}
