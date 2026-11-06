import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import App from "@/App";
import { I18nProvider } from "@/i18n/I18nProvider";
import { jsonResponse, loadCatalog } from "./vault-settings-harness";

const vaults = [{ id: 1, slug: "first", name: "First" }, { id: 2, slug: "second", name: "Second" }];
describe("essential Owner app routing", () => {
  const calls: string[] = [];
  let blocked = false;
  beforeEach(() => {
    blocked = false;
    calls.length = 0; localStorage.clear(); resetApiClientForTests(); window.history.replaceState({}, "", "/");
    configureApiClient({ fetch: vi.fn(async (input) => {
      const url = String(input); calls.push(url);
      if (url.startsWith("/api/me")) return jsonResponse({ id: 1, display_name: "Owner", csrf_token: "csrf", auth_method: "local", locale: "en", locales: ["en","it"], vault: { ...vaults[0], delete_enabled: true, cloud_deletion_enabled: true } });
      if (url.startsWith("/api/vaults")) return jsonResponse({ items: vaults });
      if (url.startsWith("/api/i18n/catalog")) return jsonResponse({ locale: "en", locales: ["en","it"], messages: loadCatalog() });
      if (url.startsWith("/api/upload-activity")) {
        const idle = { files: 0, bytes: 0, transferred_bytes: 0 };
        return jsonResponse({ observed_at: "2026-11-06T10:00:00Z", global: { stability_wait: idle, queued: idle, uploading: { files: 13, bytes: 1000, transferred_bytes: 250 }, verifying: idle, awaiting_admission_files: 0, next_stable_at: null }, vaults: [] });
      }
      if (url.startsWith("/api/problems") || url.startsWith("/api/general/vaults") || url.startsWith("/api/general/source-volumes")) return jsonResponse({ items: [] });
      if (url.startsWith("/api/general")) return jsonResponse({ vaults: [], open_problem_count: 0, month: "2026-11", monthly_estimate: 0, previous_month_delta: null, currency: "EUR", email_configured: false, last_verified_backup: null, cloud_preflight: blocked ? { status: "blocked", remediation: "Grant s3:GetBucketPublicAccessBlock", next_probe_at: null } : undefined });
      if (url.startsWith("/api/files")) return jsonResponse({ items: [{ type: "file", name: "first.jpg", path: "first.jpg", state: "protected" }], total: 1 });
      if (url.startsWith("/api/notifications")) return jsonResponse({ items: [], unread_count: 0 });
      return jsonResponse({});
    }) });
  });
  afterEach(resetApiClientForTests);
  function show() { render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider><App /></I18nProvider></ApiQueryProvider>); }
  it.each(["/", "/general", "/problems", "/vaults/new", "/?vault_id=99"])("keeps global upload activity visible on %s", async (path) => {
    window.history.replaceState({}, "", path); show();
    expect(await screen.findByText("13 files uploading")).toBeInTheDocument();
    expect(screen.getByTestId("upload-activity")).toBeInTheDocument();
  });
  it("puts the browser first without superseded dashboard statistics or administration", async () => {
    show(); await screen.findByRole("heading", { level: 1, name: "First" }); await screen.findAllByText("first.jpg");
    expect(screen.queryByTestId("stats-compact")).not.toBeInTheDocument();
    expect(screen.getByTestId("file-browser")).toBeInTheDocument();
    expect(calls.some((url) => url.startsWith("/api/files?") && url.endsWith("vault_id=1"))).toBe(true);
  });
  it("shows one actionable installation warning without blocking local browsing", async () => {
    blocked = true; show();
    await screen.findByText("Grant s3:GetBucketPublicAccessBlock");
    expect(screen.getAllByRole("heading", { name: "Cloud work is waiting for S3 readiness" })).toHaveLength(1);
    expect(screen.getByTestId("file-browser")).toBeInTheDocument();
    expect(screen.getByText(/automatically within 60 seconds/)).toBeInTheDocument();
  });
  it("home uses the remembered Vault root without changing shared Session selection", async () => {
    localStorage.setItem("frostvault.home.1", "2"); show();
    await screen.findByRole("heading", { level: 1, name: "Second" });
    await waitFor(() => expect(calls.some((url) => url.startsWith("/api/files?") && url.endsWith("vault_id=2"))).toBe(true));
    expect(calls.some((url) => url.includes("/select"))).toBe(false);
  });
  it("an invalid explicit Vault does not fall back to another folder", async () => {
    window.history.replaceState({}, "", "/?vault_id=99"); show();
    await screen.findByRole("alert"); expect(screen.queryByTestId("file-browser")).not.toBeInTheDocument();
  });
});
