interface RuntimeConfig {
  apiBaseUrl?: string;
}

declare global {
  interface Window {
    __KATILIM_ANALIZ_CONFIG__?: RuntimeConfig;
  }
}

const DEFAULT_API_BASE_URL = "/api/v1";

function readMetaApiBaseUrl(): string | undefined {
  if (typeof document === "undefined") {
    return undefined;
  }
  return document
    .querySelector<HTMLMetaElement>('meta[name="katilim-analiz-api-base-url"]')
    ?.content.trim();
}

export function getApiBaseUrl(): string {
  const runtimeValue =
    typeof window === "undefined" ? undefined : window.__KATILIM_ANALIZ_CONFIG__?.apiBaseUrl;
  const configured = runtimeValue?.trim() || readMetaApiBaseUrl() || import.meta.env.VITE_API_BASE_URL;
  return (configured || DEFAULT_API_BASE_URL).replace(/\/$/, "");
}

export {};
