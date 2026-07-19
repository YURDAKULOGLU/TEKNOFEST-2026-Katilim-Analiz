import { afterEach, describe, expect, it } from "vitest";

import { getApiBaseUrl } from "../src/api/runtime-config";

afterEach(() => {
  delete window.__KATILIM_ANALIZ_CONFIG__;
  document.head.querySelector('meta[name="katilim-analiz-api-base-url"]')?.remove();
});

describe("runtime configuration", () => {
  it("prefers the deployment-injected API base URL and trims its trailing slash", () => {
    window.__KATILIM_ANALIZ_CONFIG__ = { apiBaseUrl: "https://internal.test/analysis/v1/" };

    expect(getApiBaseUrl()).toBe("https://internal.test/analysis/v1");
  });

  it("uses the HTML runtime meta value when no window configuration was injected", () => {
    const meta = document.createElement("meta");
    meta.name = "katilim-analiz-api-base-url";
    meta.content = "/institution/api/v1";
    document.head.append(meta);

    expect(getApiBaseUrl()).toBe("/institution/api/v1");
  });
});
