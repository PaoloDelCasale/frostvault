import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ReauthenticationRedirectError,
  apiDownload,
  apiRequest,
  configureApiClient,
  filenameFromContentDisposition,
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

  it("coalesces concurrent local reauthentication into one password submission and replays each request once", async () => {
    let resolvePassword: (password: string | null) => void;
    const password = new Promise<string | null>((resolve) => {
      resolvePassword = resolve;
    });
    const requestPassword = vi.fn(() => password);
    const requestCounts = new Map<string, number>();

    configureApiClient({
      getAuthMethod: () => "local",
      requestPassword,
    });
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      const count = (requestCounts.get(url) ?? 0) + 1;
      requestCounts.set(url, count);
      if (url === "/api/reauth") {
        return Promise.resolve(jsonResponse({ ok: true }));
      }
      if (count === 1) {
        return Promise.resolve(jsonResponse({ error: "reauth_required" }, 403));
      }
      return Promise.resolve(jsonResponse({ url }));
    });

    const requests = Promise.all([
      apiRequest<{ url: string }>("/api/admin/first", {
        method: "POST",
        body: "{}",
      }),
      apiRequest<{ url: string }>("/api/admin/second", {
        method: "POST",
        body: "{}",
      }),
    ]);

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(requestPassword).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/admin/first",
      "/api/admin/second",
    ]);

    resolvePassword!("correct horse battery staple");

    await expect(requests).resolves.toEqual([
      { url: "/api/admin/first" },
      { url: "/api/admin/second" },
    ]);
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/admin/first",
      "/api/admin/second",
      "/api/reauth",
      "/api/admin/first",
      "/api/admin/second",
    ]);
    expect(requestCounts.get("/api/admin/first")).toBe(2);
    expect(requestCounts.get("/api/admin/second")).toBe(2);
  });

  it("uses one completed local reauthentication for a delayed 403 in the original request cohort", async () => {
    let resolveSecondResponse: (response: Response) => void;
    const delayedSecondResponse = new Promise<Response>((resolve) => {
      resolveSecondResponse = resolve;
    });
    const requestCounts = new Map<string, number>();
    const requestPassword = vi.fn(async () => "recent-password");

    configureApiClient({
      getAuthMethod: () => "local",
      requestPassword,
    });
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      const count = (requestCounts.get(url) ?? 0) + 1;
      requestCounts.set(url, count);
      if (url === "/api/reauth") return Promise.resolve(jsonResponse({ ok: true }));
      if (url === "/api/admin/second" && count === 1) {
        return delayedSecondResponse;
      }
      if (count === 1) {
        return Promise.resolve(jsonResponse({ error: "reauth_required" }, 403));
      }
      return Promise.resolve(jsonResponse({ url }));
    });

    const first = apiRequest<{ url: string }>("/api/admin/first", {
      method: "POST",
      body: "{}",
    });
    const second = apiRequest<{ url: string }>("/api/admin/second", {
      method: "POST",
      body: "{}",
    });

    await expect(first).resolves.toEqual({ url: "/api/admin/first" });
    expect(requestPassword).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/reauth")).toHaveLength(1);

    resolveSecondResponse!(jsonResponse({ error: "reauth_required" }, 403));

    await expect(second).resolves.toEqual({ url: "/api/admin/second" });
    expect(requestPassword).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/reauth")).toHaveLength(1);
    expect(requestCounts.get("/api/admin/first")).toBe(2);
    expect(requestCounts.get("/api/admin/second")).toBe(2);
  });

  it("shares a throttled outcome with a delayed 403 and lets a later request retry", async () => {
    let resolveSecondResponse: (response: Response) => void;
    const delayedSecondResponse = new Promise<Response>((resolve) => {
      resolveSecondResponse = resolve;
    });
    const requestCounts = new Map<string, number>();
    const requestPassword = vi.fn(async () => "recent-password");
    let reauthenticationAttempts = 0;

    configureApiClient({
      getAuthMethod: () => "local",
      requestPassword,
    });
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      const count = (requestCounts.get(url) ?? 0) + 1;
      requestCounts.set(url, count);
      if (url === "/api/reauth") {
        reauthenticationAttempts += 1;
        return Promise.resolve(
          reauthenticationAttempts === 1
            ? jsonResponse({ detail: "Try again later" }, 429)
            : jsonResponse({ ok: true }),
        );
      }
      if (url === "/api/admin/second" && count === 1) {
        return delayedSecondResponse;
      }
      if (count === 1) {
        return Promise.resolve(jsonResponse({ error: "reauth_required" }, 403));
      }
      return Promise.resolve(jsonResponse({ url }));
    });

    const firstFailure = apiRequest("/api/admin/first", {
      method: "POST",
      body: "{}",
    }).then(
      () => {
        throw new Error("Expected reauthentication to fail.");
      },
      (error: unknown) => error,
    );
    const secondFailure = apiRequest("/api/admin/second", {
      method: "POST",
      body: "{}",
    }).then(
      () => {
        throw new Error("Expected reauthentication to fail.");
      },
      (error: unknown) => error,
    );

    const firstError = await firstFailure;
    expect(firstError).toMatchObject({
      status: 429,
      messageKey: "ui.reauth_failed",
    });
    expect(requestPassword).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/reauth")).toHaveLength(1);

    resolveSecondResponse!(jsonResponse({ error: "reauth_required" }, 403));

    await expect(secondFailure).resolves.toBe(firstError);
    expect(requestPassword).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/reauth")).toHaveLength(1);
    expect(requestCounts.get("/api/admin/first")).toBe(1);
    expect(requestCounts.get("/api/admin/second")).toBe(1);

    await expect(
      apiRequest<{ url: string }>("/api/admin/retry", {
        method: "POST",
        body: "{}",
      }),
    ).resolves.toEqual({ url: "/api/admin/retry" });
    expect(requestPassword).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/reauth")).toHaveLength(2);
    expect(requestCounts.get("/api/admin/retry")).toBe(2);
  });

  it("rejects all concurrent callers after a throttled reauthentication and permits a later retry", async () => {
    const requestCounts = new Map<string, number>();
    const requestPassword = vi.fn(async () => "recent-password");

    configureApiClient({
      getAuthMethod: () => "local",
      requestPassword,
    });
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      const count = (requestCounts.get(url) ?? 0) + 1;
      requestCounts.set(url, count);
      if (url === "/api/reauth") {
        return Promise.resolve(
          count === 1
            ? jsonResponse({ detail: "Try again later" }, 429)
            : jsonResponse({ ok: true }),
        );
      }
      if (count === 1) {
        return Promise.resolve(jsonResponse({ error: "reauth_required" }, 403));
      }
      return Promise.resolve(jsonResponse({ url }));
    });

    const failures = await Promise.allSettled([
      apiRequest("/api/admin/first", { method: "POST", body: "{}" }),
      apiRequest("/api/admin/second", { method: "POST", body: "{}" }),
    ]);

    expect(failures).toHaveLength(2);
    for (const failure of failures) {
      expect(failure).toMatchObject({
        status: "rejected",
        reason: {
          status: 429,
          messageKey: "ui.reauth_failed",
        },
      });
    }
    if (failures[0]?.status === "rejected" && failures[1]?.status === "rejected") {
      expect(failures[0].reason).toBe(failures[1].reason);
    }
    expect(requestPassword).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/reauth")).toHaveLength(1);

    await expect(
      apiRequest<{ url: string }>("/api/admin/retry", {
        method: "POST",
        body: "{}",
      }),
    ).resolves.toEqual({ url: "/api/admin/retry" });

    expect(requestPassword).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/reauth")).toHaveLength(2);
    expect(requestCounts.get("/api/admin/retry")).toBe(2);
  });

  it("replays an authenticated binary download after local reauthentication", async () => {
    configureApiClient({
      getAuthMethod: () => "local",
      requestPassword: async () => "recent-password",
    });
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ error: "reauth_required" }, 403))
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(new Response("bytes"));

    const result = await apiDownload("/api/download", {}, "backup.bak.enc");

    expect(result.filename).toBe("backup.bak.enc");
    expect(await result.blob.text()).toBe("bytes");
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/download",
      "/api/reauth",
      "/api/download",
    ]);
    expect(
      new Headers(fetchMock.mock.calls[2]?.[1]?.headers).get("X-CSRF-Token"),
    ).toBeNull();
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

describe("apiDownload filename and checksum handling", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    configureApiClient({ fetch: fetchMock });
  });

  afterEach(() => {
    resetApiClientForTests();
  });

  it("prefers RFC 5987 names and sanitizes path/control characters", () => {
    expect(
      filenameFromContentDisposition(
        "attachment; filename=\"fallback.bak.enc\"; filename*=UTF-8''..%2Fmetadata%20backup.bak.enc",
      ),
    ).toBe(".._metadata backup.bak.enc");
    expect(filenameFromContentDisposition("attachment; filename=\"..\"", "backup.bak.enc")).toBe(
      "backup.bak.enc",
    );
  });

  it("returns the binary body and only accepts a valid SHA-256 header", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("bytes", {
        headers: {
          "Content-Disposition": "attachment; filename=backup.bak.enc",
          "X-Checksum-SHA256": "not-a-checksum",
        },
      }),
    );

    const result = await apiDownload("/download");
    expect(result.filename).toBe("backup.bak.enc");
    expect(result.checksumSha256).toBeNull();
    expect(await result.blob.text()).toBe("bytes");
  });
});

describe("apiRequest session and errors", () => {
  const fetchMock = vi.fn();
  const navigate = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    document.cookie = "frostvault_csrf=test-csrf-token";
    fetchMock.mockReset();
    navigate.mockReset();
    configureApiClient({
      fetch: fetchMock,
      navigate,
      translate: (key) =>
        key === "api.quota_exceeded"
          ? "Storage quota exceeded."
          : key === "ui.reauth_failed"
            ? "Reauthentication failed."
            : key,
    });
  });

  afterEach(() => {
    document.cookie = "frostvault_csrf=";
    resetApiClientForTests();
  });

  it("navigates to the sign-in screen on 401", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "Unauthorized" }, 401));

    await expect(apiRequest("/api/me")).rejects.toMatchObject({ status: 401 });
    expect(navigate).toHaveBeenCalledWith("/login");
  });

  it("turns an error response carrying message_key into the matching localized message", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { message_key: "api.quota_exceeded", message: "ignored fallback" },
        409,
      ),
    );

    await expect(
      apiRequest("/api/upload", { method: "POST", body: "{}" }),
    ).rejects.toMatchObject({
      message: "Storage quota exceeded.",
      messageKey: "api.quota_exceeded",
      status: 409,
    });
  });
});
