import type { QueryClient } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiQueryProvider, browseSourceVolume, configureApiClient, resetApiClientForTests } from "@/api";
import App from "@/App";
import { NotificationCenter } from "@/components/NotificationCenter";
import { SourceDirectoryBrowser } from "@/components/SourceDirectoryBrowser";
import { StopProtectionDialog } from "@/components/StopProtectionDialog";
import { VaultDeletionDialog } from "@/components/VaultDeletionDialog";
import { I18nProvider } from "@/i18n/I18nProvider";
import { RecoverSelection, type Target } from "@/pages/archive/RecoverSelection";
import { GeneralPage } from "@/pages/general/GeneralPage";
import { LoginPage } from "@/pages/login/LoginPage";
import { VaultCreatePage } from "@/pages/vault-create";
import { ThemeProvider } from "@/theme";
import { jsonResponse } from "./support/http";
import { createTestQueryClient, renderWithQueryClient } from "./support/queryClient";
import {
  createVaultAccessFetch,
  defaultBillingExport,
  loadCatalog,
  renderVaultAccess,
} from "./vault-settings-harness";

/**
 * Request cadence per screen (#800): the requests each screen makes when it
 * mounts and on its main interactions. A change may make a list shorter,
 * never longer, and never changes a URL or a method.
 */
type Reply = unknown | ((url: string, init?: RequestInit) => unknown);

/** Records `METHOD url` for every request; replies by longest matching URL prefix. */
function record(replies: Record<string, Reply> = {}): string[] {
  const calls: string[] = [];
  const messages = loadCatalog();
  const prefixes = Object.keys(replies).sort((a, b) => b.length - a.length);
  configureApiClient({ fetch: vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    calls.push(`${(init?.method ?? "GET").toUpperCase()} ${url}`);
    const match = prefixes.find((prefix) => url.startsWith(prefix));
    const reply = match === undefined ? {} : replies[match];
    return jsonResponse(typeof reply === "function" ? (reply as (url: string, init?: RequestInit) => unknown)(url, init) : reply);
  }) });
  return calls;
}

const sorted = (calls: string[]) => [...calls].sort();
/** Lets pending requests and their renders settle. */
async function settle() {
  for (let round = 0; round < 5; round++) await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

const settingsVault = {
  id: 1, uuid: "00000000-0000-4000-8000-000000000001", name: "Test Archive", source_root: "/sources/photos/a",
  s3_prefix: "", s3_bucket: "frostvault-test-ab12cd34", s3_region: "eu-south-1", dedicated_bucket: true,
  lifecycle: "active", paused: false, root_released_at: null, vault_deletion_status: null, retention_lock_days: 180,
};
const stopStatus = { vault_id: 1, vault_name: "Test Archive", stopped: false, root_released: false,
  cloud_disposition: "keep", group_id: null, jobs: [], cancellable: false, pending_until: null,
  local_files_blocking_cloud_delete: 0 };

/** `rerender` replaces the whole tree: keep the same client around it. */
function WithClient({ client, children }: { client: QueryClient; children: ReactNode }) {
  return <ApiQueryProvider client={client}>{children}</ApiQueryProvider>;
}

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); resetApiClientForTests(); });

describe("request cadence (#800)", () => {
  it("Vault settings: mount, Minimum Storage, Stop protection, Reconnect", async () => {
    const calls: string[] = [];
    const inner = createVaultAccessFetch({
      "GET /api/general/vaults": () => jsonResponse({ items: [settingsVault] }),
      "GET /api/vaults/1/retention-lock": () => jsonResponse({ vault_id: 1, days: 180, mode: "GOVERNANCE", retained: null }),
      "GET /api/vaults/1/stop-protection": () => jsonResponse(stopStatus),
      "GET /api/general/source-volumes": () => jsonResponse({ items: [{ alias: "photos", path: "/sources/photos", access: "rw", health: "ok", vault_count: 1, diagnostic: null }] }),
      "GET /api/source-volumes/photos/browse": () => jsonResponse({ volume_alias: "photos", relative_path: "", items: [] }),
    });
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (!url.startsWith("/api/i18n/catalog")) calls.push(`${(init?.method ?? "GET").toUpperCase()} ${url}`);
      return inner(String(input).replace(/\?reconnect_vault_id=\d+$/, ""), init);
    });
    renderVaultAccess({ fetchImpl });
    await screen.findByTestId("retention-lock-mode");
    await settle();
    expect(sorted(calls)).toEqual([
      "GET /api/general/vaults",
      "GET /api/vaults/1/billed-cost",
      "GET /api/vaults/1/cloud-usage",
      "GET /api/vaults/1/duplicate-cleanup",
      "GET /api/vaults/1/jobs?action=storage-class&active_only=true",
      "GET /api/vaults/1/lifecycle",
      "GET /api/vaults/1/mirror-cleanup",
      "GET /api/vaults/1/move",
      // One read for the four sections that show the policy: four before #800.
      "GET /api/vaults/1/operation-policy",
      "GET /api/vaults/1/retention-lock",
    ]);

    calls.length = 0;
    const minimum = screen.getByText("Show the summary").closest("details")!;
    minimum.open = true;
    fireEvent(minimum, new Event("toggle"));
    await screen.findByTestId("minimum-storage");
    expect(calls).toEqual(["GET /api/vaults/1/minimum-storage"]);

    // Each opening of the dialog reads the state again.
    calls.length = 0;
    fireEvent.click(screen.getByRole("button", { name: "Stop protecting this Vault" }));
    await screen.findByRole("combobox", { name: "Cloud copy" });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Stop protecting this Vault" }));
    await screen.findByRole("combobox", { name: "Cloud copy" });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(calls).toEqual(["GET /api/vaults/1/stop-protection", "GET /api/vaults/1/stop-protection"]);

    calls.length = 0;
    fireEvent.click(screen.getByRole("button", { name: "Reconnect folder" }));
    const volume = await screen.findByRole("combobox", { name: "Destination Source Volume" });
    await waitFor(() => expect(volume.querySelectorAll("option")).toHaveLength(2));
    fireEvent.change(volume, { target: { value: "photos" } });
    await screen.findByTestId("source-directory-browser");
    await settle();
    expect(calls).toEqual(["GET /api/general/source-volumes", "GET /api/source-volumes/photos/browse?reconnect_vault_id=1"]);
  });

  it("System page: mount and the Recovery Kit's 30 s refresh", async () => {
    const calls = record({
      "/api/recovery-kit": { configured: false, awaiting_backup: false, system_bucket: null, fingerprint: null, custody_acknowledged: false, metadata_restore: null },
      "/api/general/metadata-backups": { status: { last_status: "never", last_run: null, succeeded_count: 0, failed_count: 0 }, runs: [] },
      "/api/general/source-volumes": { items: [] },
      "/api/billing-export": defaultBillingExport,
    });
    vi.useFakeTimers();
    renderWithQueryClient(<I18nProvider><GeneralPage /></I18nProvider>);
    for (let tick = 0; tick < 5; tick++) await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(sorted(calls)).toEqual([
      "GET /api/billing-export",
      "GET /api/general/metadata-backups",
      "GET /api/general/source-volumes",
      "GET /api/recovery-kit",
    ]);
    calls.length = 0;
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(calls).toEqual(["GET /api/recovery-kit", "GET /api/recovery-kit"]);
  });

  it("Sign-in page: mount", async () => {
    const calls = record({ "/api/login/options": { oidc_enabled: false, oidc_provider_name: null } });
    renderWithQueryClient(<I18nProvider><ThemeProvider><LoginPage /></ThemeProvider></I18nProvider>);
    await settle();
    expect(calls).toEqual(["GET /api/login/options"]);
  });

  it("New Vault page: mount", async () => {
    const calls = record({ "/api/general/source-volumes": { items: [] } });
    renderWithQueryClient(<I18nProvider><VaultCreatePage displayName="Ada" onNavigate={vi.fn()} embedded /></I18nProvider>);
    await settle();
    expect(calls).toEqual(["GET /api/general/source-volumes"]);
  });

  it("App shell: sign-in bootstrap and the Vault's file list", async () => {
    vi.stubGlobal("EventSource", class { onerror = null; addEventListener() {} close() {} });
    window.history.replaceState({}, "", "/vaults/1/files");
    const calls = record({
      "/api/me": { id: 1, display_name: "Owner", csrf_token: "csrf", auth_method: "local", locale: "en", locales: ["en", "it"], vault: { id: 1, name: "Photos" } },
      "/api/vaults": { items: [{ id: 1, name: "Photos", uuid: "photos" }] },
      "/api/general": { vaults: [], open_problem_count: 0, month: "2026-11", monthly_estimate: 0, previous_month_delta: null, currency: "EUR" },
      "/api/general/vaults": { items: [] },
      "/api/vaults/1/files": { items: [], total: 0 },
      "/api/notifications": { items: [], unread_count: 0 },
      "/api/vaults/1/jobs": { groups: [] },
    });
    const client = createTestQueryClient();
    renderWithQueryClient(<I18nProvider><ThemeProvider><App /></ThemeProvider></I18nProvider>, { client });
    await screen.findByTestId("file-list-empty");
    await settle();
    expect(sorted(calls)).toEqual([
      "GET /api/general",
      "GET /api/general/vaults",
      "GET /api/me",
      "GET /api/notifications?limit=50&status=unread",
      "GET /api/upload-activity",
      "GET /api/vaults",
      "GET /api/vaults/1/excluded-folders?directory=",
      "GET /api/vaults/1/files?q=&state=&directory=&page=1&page_size=100",
      "GET /api/vaults/1/jobs?action=recover&action=free-space&active_only=true",
      "GET /api/vaults/1/operation-policy",
      "GET /api/vaults/1/stats",
    ]);
    // A blanket refresh (Overview, when a bucket becomes ready) reads the
    // lists again, never the sign-in bootstrap.
    calls.length = 0;
    await act(async () => { await client.invalidateQueries(); });
    await settle();
    expect(calls).not.toContain("GET /api/me");
    expect(calls).not.toContain("GET /api/vaults");
    expect(calls).toContain("GET /api/general");
    cleanup(); client.clear();
    window.history.replaceState({}, "", "/");
  });

  it("Recover dialog: mount and a change of retrieval speed", async () => {
    const target: Target = { vaultId: 2, vaultName: "Second", item: { type: "file", name: "same.jpg", path: "same.jpg", state: "cloud_only" } };
    const calls = record({
      "/api/vaults/2/operation-policy": { local_release_after_days: 180 },
      "/api/vaults/2/recover/estimate": { archive_version_id: "v", requires_restore: true, estimate: { estimated_cost_eur: 1, estimated_hours: 48, tier: "Bulk", days: 3 } },
    });
    const t = (key: string) => key;
    renderWithQueryClient(<RecoverSelection targets={[target]} t={t} onClose={vi.fn()} onDone={vi.fn()} />);
    const speed = await screen.findByRole("combobox");
    expect(sorted(calls)).toEqual(["GET /api/vaults/2/operation-policy", "POST /api/vaults/2/recover/estimate"]);
    calls.length = 0;
    fireEvent.change(speed, { target: { value: "Standard" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "ui.row_action_recover" })).toBeEnabled());
    expect(sorted(calls)).toEqual(["GET /api/vaults/2/operation-policy", "POST /api/vaults/2/recover/estimate"]);
  });

  it("Source directory browser: mount, a folder, back to the root", async () => {
    const calls = record({
      "/api/source-volumes/photos/browse": (url: string) => ({ volume_alias: "photos", relative_path: "",
        items: url.includes("path=") ? [] : [{ name: "albums", relative_path: "albums", navigable: true, selectable: true, occupation: null }] }),
    });
    renderWithQueryClient(<I18nProvider><SourceDirectoryBrowser volumeAlias="photos" browse={browseSourceVolume} selectedPath={null} onSelect={vi.fn()} /></I18nProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "albums" }));
    await screen.findByText("No folders here.");
    fireEvent.click(screen.getByRole("button", { name: "/sources/photos" }));
    await screen.findByRole("button", { name: "albums" });
    expect(calls).toEqual([
      "GET /api/source-volumes/photos/browse",
      "GET /api/source-volumes/photos/browse?path=albums",
      "GET /api/source-volumes/photos/browse",
    ]);
  });

  it("Stop protection dialog: each opening and «Refresh»", async () => {
    const calls = record({ "/api/vaults/1/stop-protection": { ...stopStatus, stopped: true } });
    const dialog = (open: boolean) => <I18nProvider><StopProtectionDialog open={open} onOpenChange={vi.fn()} vaultId={1} vaultName="Photos" /></I18nProvider>;
    const client = createTestQueryClient();
    const view = renderWithQueryClient(dialog(true), { client });
    fireEvent.click(await screen.findByRole("button", { name: "Refresh status" }));
    await settle();
    expect(calls).toEqual(["GET /api/vaults/1/stop-protection", "GET /api/vaults/1/stop-protection"]);
    calls.length = 0;
    view.rerender(<WithClient client={client}>{dialog(false)}</WithClient>);
    await settle();
    view.rerender(<WithClient client={client}>{dialog(true)}</WithClient>);
    await screen.findByTestId("stop-protection-status");
    expect(calls).toEqual(["GET /api/vaults/1/stop-protection"]);
  });

  it("Vault Deletion dialog: opening reads the state, then the preview", async () => {
    const calls = record({
      "/api/vaults/2/vault-deletion": { vault_id: 2, vault_name: "Photos", lifecycle: "tombstone", deletable: true, local_files: 0, request: null },
      "/api/vaults/2/vault-deletion/preview": { prefix: "", object_count: 0, version_count: 0, delete_marker_count: 0, multipart_upload_count: 0,
        byte_count: 0, early_deletion_cost: 0, currency: "EUR", pricing_effective_at: "2026-01-01", region: "eu-south-1",
        executes_at: "2026-11-23T12:00:00+00:00", delay_seconds: 0, by_storage_class: [] },
    });
    renderWithQueryClient(<I18nProvider><VaultDeletionDialog open onOpenChange={vi.fn()} vaultId={2} vaultName="Photos" /></I18nProvider>);
    await waitFor(() => expect(calls).toHaveLength(2));
    await settle();
    expect(calls).toEqual(["GET /api/vaults/2/vault-deletion", "GET /api/vaults/2/vault-deletion/preview"]);
  });

  it("Notification center: mount, opening, read history and «Load more»", async () => {
    const item = (id: number, read: boolean) => ({ id, user_id: 7, vault_id: 9, job_id: 44, event: "recovery_completed", title: `Notification ${id}`,
      body: `Body ${id}`, title_key: null, body_key: null, message_params: {}, in_app_enabled: true, dedupe_key: null,
      created_at: "2026-11-10T10:00:00+00:00", read, read_at: read ? "2026-11-10T11:00:00+00:00" : null });
    const calls = record({
      "/api/notifications": (url: string) => url.includes("status=read")
        ? { items: [item(url.includes("before_id") ? 3 : 4, true)], unread_count: 1, has_more: !url.includes("before_id") }
        : { items: [item(url.includes("before_id") ? 8 : 9, false)], unread_count: 2, has_more: !url.includes("before_id") },
    });
    const client = createTestQueryClient();
    renderWithQueryClient(<NotificationCenter queryClient={client} />, { client });
    await settle();
    expect(calls).toEqual(["GET /api/notifications?limit=50&status=unread"]);
    calls.length = 0;
    fireEvent.click(screen.getByTestId("notification-bell"));
    await screen.findByText("Notification 9");
    fireEvent.click(screen.getByTestId("notifications-load-more-unread"));
    await screen.findByText("Notification 8");
    fireEvent.click(screen.getByTestId("notifications-toggle-read"));
    await screen.findByText("Notification 4");
    fireEvent.click(screen.getByTestId("notifications-load-more"));
    await screen.findByText("Notification 3");
    await settle();
    expect(calls).toEqual([
      "GET /api/notifications?limit=50&status=unread&before_id=9",
      "GET /api/notifications?limit=30&status=read",
      "GET /api/notifications?limit=30&status=read&before_id=4",
    ]);
    cleanup(); client.clear();
  });
});
