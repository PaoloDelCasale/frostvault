import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  apiRequest,
  configureApiClient,
  resetApiClientForTests,
} from "@/api";
import { I18nContext, type I18nContextValue } from "@/i18n/context";
import { ReauthPasswordGate } from "@/pages/archive/ReauthPasswordGate";

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderGate() {
  const messages: Record<string, string> = {
    "ui.reauth_password_title": "Reauthenticate",
    "ui.reauth_password_description": "Confirm your password.",
    "login.password": "Password",
    "ui.cancel": "Cancel",
    "ui.reauth_password_submit": "Confirm",
  };
  const i18n: I18nContextValue = {
    locale: "en",
    locales: ["en", "it"],
    ready: true,
    t: (key) => messages[key] ?? key,
    setLocale: vi.fn(async () => undefined),
  };

  return render(
    <I18nContext.Provider value={i18n}>
      <ReauthPasswordGate>
        <div>Archive</div>
      </ReauthPasswordGate>
    </I18nContext.Provider>,
  );
}

function reauthCalls(fetchMock: ReturnType<typeof vi.fn>): unknown[][] {
  return fetchMock.mock.calls.filter(([url]) => url === "/api/reauth");
}

describe("ReauthPasswordGate", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    configureApiClient({
      fetch: fetchMock,
      getAuthMethod: () => "local",
    });
  });

  afterEach(() => {
    cleanup();
    resetApiClientForTests();
  });

  it("settles concurrent API callers after one password submit and one reauthentication POST", async () => {
    const requestCounts = new Map<string, number>();
    let resolveReauthentication: (response: Response) => void;
    const reauthenticationResponse = new Promise<Response>((resolve) => {
      resolveReauthentication = resolve;
    });

    fetchMock.mockImplementation((input) => {
      const url = String(input);
      const count = (requestCounts.get(url) ?? 0) + 1;
      requestCounts.set(url, count);
      if (url === "/api/reauth") return reauthenticationResponse;
      if (count === 1) {
        return Promise.resolve(jsonResponse({ error: "reauth_required" }, 403));
      }
      return Promise.resolve(jsonResponse({ url }));
    });
    renderGate();

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

    const passwordInput = await screen.findByTestId("reauth-password-input");
    await waitFor(() => {
      expect(requestCounts.get("/api/admin/first")).toBe(1);
      expect(requestCounts.get("/api/admin/second")).toBe(1);
    });
    fireEvent.change(passwordInput, { target: { value: "recent-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(reauthCalls(fetchMock)).toHaveLength(1));
    await new Promise((resolve) => setTimeout(resolve, 0));
    resolveReauthentication!(jsonResponse({ ok: true }));

    await expect(requests).resolves.toEqual([
      { url: "/api/admin/first" },
      { url: "/api/admin/second" },
    ]);
    expect(reauthCalls(fetchMock)).toHaveLength(1);
    expect(requestCounts.get("/api/admin/first")).toBe(2);
    expect(requestCounts.get("/api/admin/second")).toBe(2);
  });

  it("rejects every waiting API caller when the password dialog is cancelled", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(jsonResponse({ error: "reauth_required" }, 403)),
    );
    renderGate();

    const requests = Promise.allSettled([
      apiRequest("/api/admin/first", { method: "POST", body: "{}" }),
      apiRequest("/api/admin/second", { method: "POST", body: "{}" }),
    ]);

    await screen.findByTestId("reauth-password-input");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    const results = await requests;
    expect(results).toHaveLength(2);
    for (const result of results) {
      expect(result).toMatchObject({
        status: "rejected",
        reason: expect.any(ApiError),
      });
      if (result.status === "rejected") {
        expect(result.reason).toMatchObject({
          status: 403,
          messageKey: "ui.reauth_failed",
        });
      }
    }
    expect(reauthCalls(fetchMock)).toHaveLength(0);
  });

  it("rejects every waiting API caller when the password gate unmounts", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(jsonResponse({ error: "reauth_required" }, 403)),
    );
    const view = renderGate();

    const requests = Promise.allSettled([
      apiRequest("/api/admin/first", { method: "POST", body: "{}" }),
      apiRequest("/api/admin/second", { method: "POST", body: "{}" }),
    ]);

    await screen.findByTestId("reauth-password-input");
    view.unmount();

    const results = await requests;
    expect(results).toHaveLength(2);
    for (const result of results) {
      expect(result).toMatchObject({
        status: "rejected",
        reason: expect.any(ApiError),
      });
      if (result.status === "rejected") {
        expect(result.reason).toMatchObject({
          status: 403,
          messageKey: "ui.reauth_failed",
        });
      }
    }
    expect(reauthCalls(fetchMock)).toHaveLength(0);
  });

  it("opens a later prompt after a failed reauthentication attempt", async () => {
    const requestCounts = new Map<string, number>();
    let reauthenticationAttempts = 0;
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
      if (count === 1) {
        return Promise.resolve(jsonResponse({ error: "reauth_required" }, 403));
      }
      return Promise.resolve(jsonResponse({ url }));
    });
    renderGate();

    const failedRequest = apiRequest("/api/admin/first", {
      method: "POST",
      body: "{}",
    });
    const firstPasswordInput = await screen.findByTestId("reauth-password-input");
    fireEvent.change(firstPasswordInput, { target: { value: "recent-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await expect(failedRequest).rejects.toMatchObject({
      status: 429,
      messageKey: "ui.reauth_failed",
    });
    expect(reauthCalls(fetchMock)).toHaveLength(1);

    const retry = apiRequest<{ url: string }>("/api/admin/retry", {
      method: "POST",
      body: "{}",
    });
    const retryPasswordInput = await screen.findByTestId("reauth-password-input");
    fireEvent.change(retryPasswordInput, { target: { value: "recent-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await expect(retry).resolves.toEqual({ url: "/api/admin/retry" });
    expect(reauthCalls(fetchMock)).toHaveLength(2);
    expect(requestCounts.get("/api/admin/first")).toBe(1);
    expect(requestCounts.get("/api/admin/retry")).toBe(2);
  });
});
