import { ApiProblem } from "../api/client";

interface LoadingStateProps {
  label: string;
}

export function LoadingState({ label }: LoadingStateProps) {
  return (
    <div className="state-card state-card--loading" role="status" aria-label={label}>
      <span className="loading-mark" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

interface ErrorStateProps {
  error: unknown;
  onRetry?: () => void;
  title?: string;
}

export function ErrorState({ error, onRetry, title = "Veri alınamadı" }: ErrorStateProps) {
  const problem = error instanceof ApiProblem ? error : null;
  const detail = problem?.message || "Beklenmeyen bir bağlantı hatası oluştu.";

  return (
    <div className="state-card state-card--error" role="alert">
      <strong>{title}</strong>
      <p>{detail}</p>
      {problem?.correlationId ? (
        <p className="technical-reference">İzleme kodu: {problem.correlationId}</p>
      ) : null}
      {onRetry ? (
        <button className="button button--secondary" type="button" onClick={onRetry}>
          Yeniden dene
        </button>
      ) : null}
    </div>
  );
}
