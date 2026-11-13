import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import App from "@/App";
import { I18nProvider } from "@/i18n/I18nProvider";
import { jsonResponse, loadCatalog } from "./vault-settings-harness";

const vaults = [{ id: 1, uuid: "first", name: "First" }, { id: 2, uuid: "second", name: "Second" }];
const location = () => `${window.location.pathname}${window.location.search}`;

describe("Owner app routing and shell (#512)", () => {
  const calls: string[] = [];
  let blocked = false;
  let reloads = 0;
  beforeEach(() => {
    blocked = false; reloads = 0;
    calls.length = 0; localStorage.clear(); resetApiClientForTests(); window.history.replaceState({}, "", "/");
    window.addEventListener("beforeunload", () => { reloads += 1; });
    configureApiClient({ fetch: vi.fn(async (input) => {
      const url = String(input); calls.push(url);
      if (url.startsWith("/api/me")) return jsonResponse({ id: 1, display_name: "Owner", csrf_token: "csrf", auth_method: "local", locale: "en", locales: ["en","it"], vault: { ...vaults[0], delete_enabled: true, cloud_deletion_enabled: true } });
      if (url.startsWith("/api/vaults")) return jsonResponse({ items: vaults });
      if (url.startsWith("/api/i18n/catalog")) return jsonResponse({ locale: "en", locales: ["en","it"], messages: loadCatalog() });
      if (url.startsWith("/api/upload-activity")) {
        const idle = { files: 0, bytes: 0, transferred_bytes: 0 };
        return jsonResponse({ observed_at: "2026-11-06T10:00:00Z", global: { stability_wait: idle, queued: idle, uploading: { files: 13, bytes: 1000, transferred_bytes: 250 }, verifying: idle, awaiting_admission_files: 0, next_stable_at: null }, vaults: [] });
      }
      if (url.startsWith("/api/stats")) {
        const second = url.endsWith("vault_id=2");
        return jsonResponse({ states: second ? { protected: 5 } : { protected: 3, pending: 2 }, protection_state: second ? "protected" : "pending", protection_suspended: false, storage: { local_bytes: 2048, cloud_bytes: 1024 }, active_jobs: 0, last_known_states: {}, runtime: {}, source_volume: {}, delete_enabled: true });
      }
      if (url.startsWith("/api/problems") || url.startsWith("/api/general/vaults") || url.startsWith("/api/general/source-volumes")) return jsonResponse({ items: [] });
      if (url.startsWith("/api/general")) return jsonResponse({ vaults: [{ id: 1, uuid: "first", name: "First", lifecycle: "active", monthly_estimate: 0.5, open_problem_count: 1 }, { id: 2, uuid: "second", name: "Second", lifecycle: "active", monthly_estimate: 0.25, open_problem_count: 0 }], open_problem_count: 1, month: "2026-11", monthly_estimate: 0.75, previous_month_delta: null, currency: "EUR", email_configured: false, last_verified_backup: null, cloud_preflight: blocked ? { status: "blocked", remediation: "Grant s3:GetBucketPublicAccessBlock", next_probe_at: null } : undefined });
      if (url.startsWith("/api/files")) return jsonResponse({ items: [{ type: "file", name: "first.jpg", path: "first.jpg", state: "protected" }], total: 1 });
      if (url.startsWith("/api/notifications")) return jsonResponse({ items: [], unread_count: 0 });
      return jsonResponse({});
    }) });
  });
  afterEach(resetApiClientForTests);
  function show() { render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider><App /></I18nProvider></ApiQueryProvider>); }

  it.each(["/", "/system", "/problems", "/vaults/new", "/vaults/1/files", "/vaults/1/settings"])("shows upload activity in the header on %s", async (path) => {
    window.history.replaceState({}, "", path); show();
    const indicator = await screen.findByTestId("upload-indicator");
    expect(within(screen.getByRole("banner")).getByTestId("upload-indicator")).toBe(indicator);
    expect(indicator).toHaveAccessibleName(/^13 files/);
  });

  it("opens on the Overview: one row per Vault, totals and no file browser", async () => {
    show();
    expect(await screen.findByRole("heading", { level: 1, name: "Overview" })).toBeInTheDocument();
    const table = screen.getByTestId("overview-table");
    await waitFor(() => expect(within(table).getAllByTestId("overview-row")).toHaveLength(2));
    expect(await within(screen.getByTestId("tile-protected")).findByText("8")).toBeInTheDocument();
    expect(within(screen.getByTestId("tile-pending")).getByText("2")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "First" })).toHaveAttribute("href", "/vaults/1/files");
    expect(screen.queryByTestId("file-browser")).not.toBeInTheDocument();
  });

  it("lists the Vaults in the header switcher, not in the sidebar", async () => {
    show();
    const navigation = await screen.findByRole("navigation", { name: "Main navigation" });
    expect(within(navigation).queryByText("Second")).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByTestId("vault-switcher"));
    await userEvent.setup().click(await screen.findByRole("button", { name: "Second" }));
    expect(location()).toBe("/vaults/2/files");
    expect(await screen.findByRole("heading", { level: 1, name: "Second" })).toBeInTheDocument();
  });

  it.each([
    ["/?vault_id=2&directory=a&file=a%2Fb.jpg", "/vaults/2/files?directory=a&file=a%2Fb.jpg"],
    ["/?vault_id=1&settings=1", "/vaults/1/settings"],
    ["/general", "/system"],
  ])("redirects the legacy address %s", async (legacy, current) => {
    window.history.replaceState({}, "", legacy); show();
    await waitFor(() => expect(location()).toBe(current));
  });

  it("navigates between sections without reloading the document", async () => {
    show(); await screen.findByRole("heading", { level: 1, name: "Overview" });
    const user = userEvent.setup();
    const navigation = screen.getByRole("navigation", { name: "Main navigation" });
    await user.click(within(navigation).getByRole("link", { name: "Problems: 1 open" }));
    expect(location()).toBe("/problems");
    expect(await screen.findByRole("heading", { level: 1, name: "Problems" })).toBeInTheDocument();
    await user.click(within(navigation).getByRole("link", { name: "System" }));
    expect(await screen.findByRole("heading", { level: 1, name: "System" })).toBeInTheDocument();
    expect(reloads).toBe(0);
  });

  it("System holds installation settings only, not the Vault list (#515)", async () => {
    window.history.replaceState({}, "", "/system"); show();
    expect(await screen.findByRole("heading", { level: 1, name: "System" })).toBeInTheDocument();
    for (const name of ["Recovery Kit", "Email notifications", "Failed uploads"]) expect(screen.getByRole("heading", { level: 2, name })).toBeInTheDocument();
    expect(await screen.findByTestId("email-not-configured")).toBeInTheDocument();
    const main = screen.getByRole("main");
    expect(within(main).queryByText("Second")).not.toBeInTheDocument();
    expect(within(main).queryByRole("link", { name: /new vault|protect a folder/i })).not.toBeInTheDocument();
    expect(within(main).queryByRole("button", { name: /reconnect/i })).not.toBeInTheDocument();
  });

  it("shows the open problem count in the sidebar, not as a header button", async () => {
    show();
    expect(await screen.findByTestId("nav-problem-count")).toHaveTextContent("1");
    expect(within(screen.getByRole("banner")).queryByRole("button", { name: /problems/i })).not.toBeInTheDocument();
  });

  it("shows one actionable installation warning without blocking local browsing", async () => {
    blocked = true; window.history.replaceState({}, "", "/vaults/1/files"); show();
    await screen.findByText("Grant s3:GetBucketPublicAccessBlock");
    expect(screen.getAllByRole("heading", { name: "Cloud work is waiting for S3 readiness" })).toHaveLength(1);
    expect(screen.getByTestId("file-browser")).toBeInTheDocument();
  });

  it("points the sidebar at the remembered Vault without changing shared Session selection", async () => {
    localStorage.setItem("frostvault.home.1", "2"); show();
    const navigation = await screen.findByRole("navigation", { name: "Main navigation" });
    expect(within(navigation).getByRole("link", { name: "Files" })).toHaveAttribute("href", "/vaults/2/files");
    expect(within(navigation).getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/vaults/2/settings");
    expect(calls.some((url) => url.includes("/select"))).toBe(false);
  });

  it("an invalid explicit Vault does not fall back to another Vault", async () => {
    window.history.replaceState({}, "", "/vaults/99/files"); show();
    await screen.findByRole("alert"); expect(screen.queryByTestId("file-browser")).not.toBeInTheDocument();
  });
});
