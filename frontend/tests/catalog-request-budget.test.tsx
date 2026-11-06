import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import App from "@/App";
import { I18nProvider } from "@/i18n/I18nProvider";
import { jsonResponse, loadCatalog } from "./vault-settings-harness";

// Exercise App's subscription AND the actual mounted FileBrowser/NotificationCenter
// observers, not just a spy on invalidateQueries (which misses polling fan-out).
describe("catalog event request budget", () => {
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); resetApiClientForTests(); });
  it("bounds a sustained upload burst to 30 requests/min/endpoint and flushes the tail within 5s", async () => {
    const listeners = new Map<string, (event: MessageEvent<string>) => void>();
    vi.stubGlobal("EventSource", class {
      onerror = null;
      addEventListener(type: string, listener: (event: MessageEvent<string>) => void) { listeners.set(type, listener); }
      close() {}
    });
    window.history.replaceState({}, "", "/?vault_id=1&directory=photos");
    const calls: string[] = [];
    configureApiClient({ fetch: vi.fn(async (input) => {
      const url = String(input); calls.push(url);
      if (url.startsWith("/api/me")) return jsonResponse({ id: 1, display_name: "Owner", csrf_token: "csrf", auth_method: "local", locale: "en", locales: ["en", "it"], vault: { id: 1, name: "Photos" } });
      if (url.startsWith("/api/vaults")) return jsonResponse({ items: [{ id: 1, name: "Photos", slug: "photos" }] });
      if (url.startsWith("/api/i18n/catalog")) return jsonResponse({ locale: "en", locales: ["en", "it"], messages: loadCatalog() });
      if (url.startsWith("/api/general")) return jsonResponse({ vaults: [], open_problem_count: 0, month: "2026-11", monthly_estimate: 0, previous_month_delta: null, currency: "EUR" });
      if (url.startsWith("/api/files")) return jsonResponse({ items: [], total: 0 });
      if (url.startsWith("/api/notifications")) return jsonResponse({ items: [], unread_count: 0 });
      return jsonResponse({ groups: [] });
    }) });
    const client = createAppQueryClient();
    render(<ApiQueryProvider client={client}><I18nProvider><App /></I18nProvider></ApiQueryProvider>);
    await screen.findByTestId("file-list-empty");
    expect(listeners.has("catalog")).toBe(true);
    vi.useFakeTimers();
    calls.length = 0;
    const emit = (revision: number) => listeners.get("catalog")!({ data: JSON.stringify({ vault_id: 1, revision, domains: ["files", "stats"], has_gap: false }) } as MessageEvent<string>);
    for (let revision = 1; revision <= 600; revision++) {
      await act(async () => { emit(revision); await vi.advanceTimersByTimeAsync(100); });
    }
    for (const endpoint of ["/api/general", "/api/notifications", "/api/files?"]) {
      const count = calls.filter((url) => url.startsWith(endpoint)).length;
      expect(count, `${endpoint} requests in 60s`).toBeLessThanOrEqual(30);
      expect(count).toBeGreaterThan(0);
    }
    const before = calls.length;
    await act(async () => { emit(601); await vi.advanceTimersByTimeAsync(5_000); });
    expect(calls.length).toBeGreaterThan(before);
    cleanup(); client.clear();
  });
});
