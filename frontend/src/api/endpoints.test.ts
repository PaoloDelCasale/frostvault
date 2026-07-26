import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configureApiClient, resetApiClientForTests } from "./client";
import { fetchI18nCatalog, fetchMe, fetchVaults } from "./endpoints";

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("foundation endpoint helpers", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    configureApiClient({ fetch: fetchMock });
  });

  afterEach(() => {
    resetApiClientForTests();
  });

  it("fetchMe caches csrf_token and auth_method for later mutating calls", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({
          id: 1,
          username: "ada",
          display_name: "Ada",
          is_admin: false,
          active: true,
          session_version: 1,
          csrf_token: "from-me",
          auth_method: "local",
          locale: "en",
          locales: ["en", "it"],
          vault: null,
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ ok: true }));

    await fetchMe();
    document.cookie = "frostvault_csrf=cookie-should-not-win";
    const { apiRequest } = await import("./client");
    await apiRequest("/api/scan", { method: "POST", body: "{}" });

    expect(new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get("X-CSRF-Token")).toBe(
      "from-me",
    );
  });

  it("fetchVaults and fetchI18nCatalog hit the foundation routes", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(
        jsonResponse({
          locale: "en",
          locales: ["en", "it"],
          messages: { "api.locale_updated": "Locale updated." },
        }),
      );

    await expect(fetchVaults()).resolves.toEqual({ items: [] });
    const catalog = await fetchI18nCatalog("en");
    expect(catalog.messages["api.locale_updated"]).toBe("Locale updated.");
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/vaults");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/i18n/catalog?locale=en");
  });
});
