import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  apiRequest,
  configureApiClient,
  resetApiClientForTests,
} from "./client";

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("apiRequest CSRF", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    document.cookie = "frostvault_csrf=test-csrf-token";
    fetchMock.mockReset();
    configureApiClient({ fetch: fetchMock });
  });

  afterEach(() => {
    document.cookie = "frostvault_csrf=";
    resetApiClientForTests();
  });

  it("attaches X-CSRF-Token to mutating requests but not to GET", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ ok: true }));

    await apiRequest("/api/scan", { method: "POST", body: "{}" });
    await apiRequest("/api/me");

    const postCall = fetchMock.mock.calls[0];
    const getCall = fetchMock.mock.calls[1];
    expect(postCall?.[0]).toBe("/api/scan");
    expect(new Headers(postCall?.[1]?.headers).get("X-CSRF-Token")).toBe(
      "test-csrf-token",
    );
    expect(getCall?.[0]).toBe("/api/me");
    expect(new Headers(getCall?.[1]?.headers).get("X-CSRF-Token")).toBeNull();
  });
});
