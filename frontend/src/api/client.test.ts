import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ReauthenticationRedirectError,
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

describe("apiRequest Reauthentication", () => {
  const fetchMock = vi.fn();
  const navigate = vi.fn();
  const requestPassword = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    document.cookie = "frostvault_csrf=test-csrf-token";
    fetchMock.mockReset();
    navigate.mockReset();
    requestPassword.mockReset();
    configureApiClient({
      fetch: fetchMock,
      navigate,
      requestPassword,
      getAuthMethod: () => "oidc",
      getPathname: () => "/archive",
      getSearch: () => "?directory=docs",
    });
  });

  afterEach(() => {
    document.cookie = "frostvault_csrf=";
    resetApiClientForTests();
  });

  it("starts OIDC Reauthentication redirect and does not treat the request as a silent failure", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: "reauth_required" }, 403),
    );

    await expect(
      apiRequest("/api/admin/users", { method: "POST", body: "{}" }),
    ).rejects.toBeInstanceOf(ReauthenticationRedirectError);

    expect(navigate).toHaveBeenCalledWith(
      "/auth/oidc/reauth?return_to=%2Farchive%3Fdirectory%3Ddocs",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("opens Break-glass password dialog and replays the original request exactly once after POST /api/reauth", async () => {
    configureApiClient({
      getAuthMethod: () => "local",
      requestPassword: async () => "correct horse battery staple",
    });
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ error: "reauth_required" }, 403))
      .mockResolvedValueOnce(jsonResponse({ message_key: "api.reauthenticated" }))
      .mockResolvedValueOnce(jsonResponse({ items: ["ok"] }));

    const result = await apiRequest<{ items: string[] }>("/api/admin/users", {
      method: "POST",
      body: JSON.stringify({ display_name: "Ada" }),
    });

    expect(result).toEqual({ items: ["ok"] });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/admin/users");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/reauth");
    expect(fetchMock.mock.calls[1]?.[1]?.body).toBe(
      JSON.stringify({ password: "correct horse battery staple" }),
    );
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/admin/users");
    expect(fetchMock.mock.calls[2]?.[1]?.body).toBe(
      JSON.stringify({ display_name: "Ada" }),
    );
  });

  it("surfaces a displayable error on failed Reauthentication without retrying forever", async () => {
    configureApiClient({
      getAuthMethod: () => "local",
      requestPassword: async () => "wrong-password",
      translate: (key) =>
        key === "ui.reauth_failed" ? "Reauthentication failed." : key,
    });
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ error: "reauth_required" }, 403))
      .mockResolvedValueOnce(jsonResponse({ detail: "Invalid credentials" }, 401));

    await expect(
      apiRequest("/api/admin/users", { method: "POST", body: "{}" }),
    ).rejects.toMatchObject({
      message: "Reauthentication failed.",
      messageKey: "ui.reauth_failed",
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/admin/users",
      "/api/reauth",
    ]);
  });
});
