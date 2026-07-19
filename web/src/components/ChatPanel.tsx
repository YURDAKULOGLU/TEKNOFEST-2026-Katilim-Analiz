import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import type { AnalysisApi } from "../api/client";
import { ErrorState } from "./AsyncState";

interface ChatPanelProps {
  client: AnalysisApi;
}

export function ChatPanel({ client }: ChatPanelProps) {
  const [question, setQuestion] = useState("");
  const chat = useMutation({
    mutationFn: (message: string) => client.sendChatMessage({ question: message }),
  });

  function submitQuestion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const message = question.trim();
    if (message.length >= 2) {
      chat.mutate(message);
    }
  }

  return (
    <section id="asistan" className="assistant-card" aria-labelledby="assistant-heading">
      <div className="assistant-intro">
        <p className="eyebrow">Yalnızca doğrulanmış kayıtlardan yanıt</p>
        <h2 id="assistant-heading">Kaynaklı asistan</h2>
        <p>
          Kampanya koşullarını sorun veya aynı bağlamdaki kayıtları karşılaştırmasını isteyin.
          Yeterli kanıt yoksa asistan yanıt üretmek yerine bunu açıkça belirtir.
        </p>
        <dl className="assistant-guardrails">
          <div>
            <dt>Veri erişimi</dt>
            <dd>İzinli, yapılandırılmış sorgular</dd>
          </div>
          <div>
            <dt>Kaynak ilkesi</dt>
            <dd>Doğrulanmış alıntı zorunlu</dd>
          </div>
          <div>
            <dt>Model profili</dt>
            <dd>Yerel ve araç yetkisiz</dd>
          </div>
        </dl>
      </div>

      <div className="chat-workspace">
        <form onSubmit={submitQuestion}>
          <label htmlFor="chat-question">Sorunuz</label>
          <textarea
            id="chat-question"
            rows={4}
            maxLength={1000}
            value={question}
            placeholder="Örn. Mobil kanaldaki güncel finansman kampanyalarının koşulları nelerdir?"
            onChange={(event) => setQuestion(event.target.value)}
          />
          <div className="chat-form-footer">
            <span>{question.length}/1000</span>
            <button
              className="button button--accent"
              type="submit"
              disabled={question.trim().length < 2 || chat.isPending}
              aria-label="Soruyu gönder"
            >
              {chat.isPending ? "Kaynaklar taranıyor…" : "Soruyu gönder"}
            </button>
          </div>
        </form>

        {chat.error ? (
          <ErrorState
            error={chat.error}
            onRetry={() => {
              const message = question.trim();
              if (message.length >= 2) chat.mutate(message);
            }}
            title="Asistan yanıt veremedi"
          />
        ) : null}

        {chat.data ? (
          <article
            className={`chat-answer ${chat.data.insufficient_evidence ? "chat-answer--abstain" : ""}`}
            aria-live="polite"
          >
            <p className="eyebrow">
              {chat.data.insufficient_evidence ? "Güvenli geri çekilme" : "Kaynaklı yanıt"}
            </p>
            <h3>{chat.data.insufficient_evidence ? "Yeterli kanıt bulunamadı" : "Yanıt"}</h3>
            <p>{chat.data.answer}</p>

            {chat.data.warnings.length > 0 ? (
              <ul className="warning-list">
                {chat.data.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            ) : null}

            {chat.data.citations.length > 0 ? (
              <section aria-labelledby="chat-citations-heading">
                <h4 id="chat-citations-heading">Yanıt kaynakları</h4>
                <ol className="citation-list">
                  {chat.data.citations.map((citation) => (
                    <li key={citation.id}>
                      <blockquote>{citation.quote}</blockquote>
                      <a href={citation.source_url} target="_blank" rel="noreferrer noopener">
                        {citation.source_title ?? "Resmî kaynak"}
                      </a>
                      <span className={`status-badge status-badge--${citation.status}`}>
                        {citation.status === "verified" ? "Doğrulandı" : "Kaynak doğrulanamadı"}
                      </span>
                    </li>
                  ))}
                </ol>
              </section>
            ) : null}
          </article>
        ) : (
          <div className="chat-placeholder">
            <strong>Yanıtlar kaynakla birlikte görünür</strong>
            <p>Asistan bir banka çalışanı değildir ve uygunluk kararı vermez.</p>
          </div>
        )}
      </div>
    </section>
  );
}
