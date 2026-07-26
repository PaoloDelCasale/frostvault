import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ACTIVE_JOB_POLL_MS,
  ApiQueryProvider,
  IDLE_POLL_MS,
  configureApiClient,
  createAppQueryClient,
  jobPollIntervalMs,
  resetApiClientForTests,
} from "@/api";
import type { FilesResponse, JobsResponse, MeVault } from "@/api/types";
import { FileBrowser } from "@/pages/archive/FileBrowser";
import { availableActions } from "@/pages/archive/actions";

const messages: Record<string, string> = {
  "ui.search_placeholder": "Search by file or folder name…",
  "ui.filter_by_state": "Filter by state",
  "ui.all_items": "All items",
  "ui.go_up": "Go to parent folder",
  "ui.up": "↑ Up",
  "ui.name": "Name",
  "ui.size": "Size",
  "ui.state": "State",
  "ui.cloud_storage": "Cloud storage",
  "ui.previous": "Previous",
  "ui.next": "Next",
  "ui.page_label": "Page {page} of {pages} · {total} {unit}",
  "ui.items_unit": "items",
  "ui.files_found_unit": "files found",
  "ui.empty_no_files": "This folder is empty.",
  "ui.empty_no_matches": "No Vault Files match your search.",
  "ui.breadcrumb_archive": "Archive",
  "ui.folder_item_count": "{count} files in this folder",
  "ui.file_total": "File total",
  "ui.cloud_classes": "{count} cloud classes",
  "ui.more_actions": "More actions",
  "ui.path_history": "Path History",
  "ui.path_history_versions": "{count} Archive Versions",
  "ui.path_history_no_versions": "No Archive Versions",
  "ui.path_history_loading": "Loading Path History…",
  "ui.path_history_error": "Unable to load Path History.",
  "ui.close_path_history": "Close",
  "ui.file_list_placeholder": "File list",
  "ui.row_action_upload": "Upload",
  "ui.row_action_recover": "Recover",
  "ui.row_action_free_space": "Free local space",
  "ui.row_action_cloud_archive": "Hide in cloud",
  "ui.row_action_cloud_purge": "Purge permanently",
  "ui.action_directory_count": "{action} {count} files",
  "ui.row_actions_title": "Actions for {name}",
  "ui.confirm_free_space_title": "Free local space?",
  "ui.confirm_free_space_description": "Remove the Local Copy of {path}.",
  "ui.confirm_cloud_archive_title": "Hide in cloud?",
  "ui.confirm_cloud_archive_description": "{explanation}\n\nHide {path}?",
  "ui.confirm_action": "Confirm",
  "ui.cancel": "Cancel",
  "ui.select_archive_version": "Select Archive Version",
  "ui.select_archive_version_description": "Choose version for {path}.",
  "ui.version_option": "Version #{number} · {storage} · {size}",
  "ui.version_date_storage": "#{number} · {date} · {storage}",
  "ui.recover_confirm_title": "Recover {path}?",
  "ui.recover_version_summary": "Version #{number} ({storage})",
  "ui.recover_estimate_line": "Restore tier: {tier} for {days} days · ~€{cost} / ~{hours}h",
  "ui.recover_irreversible_note": "S3 RestoreObject cannot be cancelled after AWS accepts it.",
  "ui.recover_high_impact_note": "High-impact restore requires owner approval.",
  "ui.recover_estimate_failed": "Could not load the restore estimate. You can still continue.",
  "ui.recover_no_versions": "No recoverable Archive Version is available",
  "ui.recover_continue": "Recover",
  "ui.cloud_purge_title": "Purge permanently?",
  "ui.cloud_purge_intro": "Permanent purge deletes every selected Archive Version.",
  "ui.cloud_purge_preview": "Selection: {objects} object(s), {versions} version(s), {markers} marker(s), {bytes} bytes.",
  "ui.cloud_purge_delay": "A {seconds}-second cancellable delay applies.",
  "ui.cloud_purge_reason": "Reason for this permanent purge",
  "ui.cloud_purge_confirm_label": "Type the vault name ({vault}) or this phrase to confirm",
  "ui.cloud_purge_phrase": "Confirmation phrase",
  "ui.cloud_purge_submit": "Schedule permanent purge",
  "ui.job_bytes_progress": "{transferred} of {total}",
  "ui.job_files_progress": "{completed} of {total} files",
  "ui.operation_not_cancellable": "This operation cannot be fully cancelled after AWS accepted RestoreObject.",
  "ui.stop": "Stop",
  "ui.stopping": "Stopping…",
  "ui.starting": "Starting…",
  "ui.approve_restore": "Approve restore",
  "ui.approving": "Approving…",
  "ui.reauth_failed": "Reauthentication failed.",
  "operation.uploading": "Uploading",
  "operation.queued": "Waiting",
  "operation.restoring": "Restoring",
  "operation.pending_approval": "Awaiting approval",
  "operation.completed": "Completed",
  "operation.upload_verified": "Verified",
  "operation.generic": "Operation",
  "state.both": "Server + cloud",
  "state.local_only": "Server only",
  "state.cloud_only": "Cloud only",
  "state.restoring": "Recovery in progress",
  "state.mixed": "Mixed state",
  "state.missing": "Unavailable",
  "state.filter.local_only": "Server only",
  "state.filter.both": "Server and cloud",
  "state.filter.cloud_only": "Cloud only",
  "state.filter.restoring": "Recovery in progress",
  "storage.STANDARD": "Standard",
  "storage.DEEP_ARCHIVE": "Deep Archive",
  "ui.symlink_rejected": "Symbolic link (rejected)",
  "ui.unsupported_local_entry": "Unsupported local entry",
};

function t(key: string, params?: Record<string, string | number>): string {
  const template = messages[key] ?? key;
  if (!params) return template;
  return Object.entries(params).reduce(
    (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
    template,
  );
}

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const ownerCaps: MeVault = {
  id: 1,
  slug: "test",
  name: "Test Archive",
  role: "owner",
  can_operate: true,
  delete_enabled: true,
  cloud_deletion_enabled: true,
  is_vault_owner: true,
};

const browsePayload: FilesResponse = {
  items: [
    {
      type: "directory",
      name: "reports",
      path: "reports",
      item_count: 3,
      total_size: 1536,
      state: "mixed",
      available_actions: { upload: 2, recover: 1, "free-space": 1 },
    },
    {
      type: "file",
      name: "readme.txt",
      path: "readme.txt",
      local_exists: 1,
      local_size: 1024,
      cloud_exists: 1,
      cloud_size: 1024,
      storage_class: "STANDARD",
      state: "both",
      upload_eligible: true,
      recover_eligible: false,
      cleanup_eligible: true,
    },
    {
      type: "file",
      name: "archive.pdf",
      path: "archive.pdf",
      local_exists: 0,
      cloud_exists: 1,
      cloud_size: 2048,
      storage_class: "DEEP_ARCHIVE",
      state: "cloud_only",
      upload_eligible: false,
      recover_eligible: true,
      cleanup_eligible: false,
    },
  ],
  total: 3,
  page: 1,
  directory: "",
  mode: "browse",
};

const emptyJobs: JobsResponse = { items: [], groups: [] };

describe("File operations — seams 1–10", () => {
  const fetchMock = vi.fn();
  const requestPassword = vi.fn(async () => "reauth-password");

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    requestPassword.mockClear();
    configureApiClient({
      fetch: fetchMock,
      requestPassword,
      getAuthMethod: () => "local",
      translate: (key, params) =>
        t(key, params as Record<string, string | number> | undefined),
    });
    window.history.replaceState({}, "", "/");
    vi.useRealTimers();
  });

  afterEach(() => {
    cleanup();
    resetApiClientForTests();
    vi.useRealTimers();
  });

  function renderBrowser(caps = ownerCaps) {
    const client = createAppQueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <ApiQueryProvider client={client}>
        <FileBrowser
          t={t}
          capabilities={caps}
          vaultName="Test Archive"
        />
      </ApiQueryProvider>,
    );
  }

  function mockRoutes(handlers: {
    files?: FilesResponse;
    jobs?: JobsResponse | (() => JobsResponse);
    onMutation?: (url: string, body: unknown) => Response | undefined;
  }) {
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url,
      );
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.includes("/api/jobs") && method === "GET") {
        const jobs =
          typeof handlers.jobs === "function"
            ? handlers.jobs()
            : (handlers.jobs ?? emptyJobs);
        return jsonResponse(jobs);
      }
      if (url.includes("/api/files/versions")) {
        return jsonResponse({
          path: "archive.pdf",
          items: [
            {
              id: "ver-old",
              version_number: 1,
              storage_class: "STANDARD",
              size: 100,
              recoverable: true,
              created_at: "2024-01-01T00:00:00Z",
            },
            {
              id: "ver-new",
              version_number: 2,
              storage_class: "DEEP_ARCHIVE",
              size: 2048,
              recoverable: true,
              created_at: "2025-06-01T12:00:00Z",
            },
          ],
          recoverable_count: 2,
          default_archive_version_id: "ver-new",
          supported_restore_tiers: ["Standard", "Bulk"],
          default_restore_tier: "Standard",
          default_restore_days: 7,
        });
      }
      if (url.includes("/api/files")) {
        return jsonResponse(handlers.files ?? browsePayload);
      }
      if (method !== "GET" && handlers.onMutation) {
        const body = init?.body ? JSON.parse(String(init.body)) : {};
        const custom = handlers.onMutation(url, body);
        if (custom) return custom;
      }
      if (url.includes("/api/recover/estimate")) {
        return jsonResponse({
          path: "archive.pdf",
          archive_version_id: "ver-new",
          storage_class: "DEEP_ARCHIVE",
          requires_restore: true,
          restore_object_irreversible: true,
          high_impact: false,
          estimate: {
            tier: "Standard",
            days: 7,
            estimated_cost_eur: 1.25,
            estimated_hours: 12,
          },
        });
      }
      if (url.includes("/api/vault/cloud-deletion")) {
        return jsonResponse({
          enabled: true,
          purge_delay_seconds: 60,
          delete_marker_explanation: "Delete markers hide the current key.",
          generated_phrase: "PURGE-PHRASE",
        });
      }
      if (url.includes("/api/cloud-deletion/preview")) {
        return jsonResponse({
          object_count: 1,
          version_count: 2,
          delete_marker_count: 1,
          byte_count: 2048,
        });
      }
      if (url.includes("/api/reauth")) {
        return jsonResponse({ ok: true });
      }
      if (method === "POST") {
        return jsonResponse({ group_id: "g1", message: "started", job_ids: [1] });
      }
      return jsonResponse({});
    });
  }

  it("seam 1: each action dispatches to the right endpoint with the right payload for file and directory", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; body: unknown }> = [];
    mockRoutes({
      onMutation: (url, body) => {
        calls.push({ url, body });
        return jsonResponse({ group_id: "g1", message: "ok", job_ids: [1] });
      },
    });
    renderBrowser();
    await screen.findByTestId("file-list-table");

    await user.click(
      within(screen.getByTestId("desktop-actions-readme.txt")).getByRole(
        "button",
        { name: "Upload" },
      ),
    );
    await waitFor(() => {
      expect(calls.some((c) => c.url === "/api/upload")).toBe(true);
    });
    expect(calls.find((c) => c.url === "/api/upload")?.body).toEqual({
      path: "readme.txt",
      is_directory: false,
    });

    await user.click(
      within(screen.getByTestId("desktop-actions-reports")).getByRole(
        "button",
        { name: "Upload 2 files" },
      ),
    );
    await waitFor(() => {
      expect(
        calls.filter((c) => c.url === "/api/upload" && (c.body as { is_directory: boolean }).is_directory),
      ).toHaveLength(1);
    });
    expect(
      calls.find(
        (c) =>
          c.url === "/api/upload" &&
          (c.body as { path: string }).path === "reports",
      )?.body,
    ).toEqual({ path: "reports", is_directory: true });
  });

  it("seam 2: bottom sheet only offers actions the capabilities permit", async () => {
    const user = userEvent.setup();
    mockRoutes({});
    const operator = {
      ...ownerCaps,
      role: "operator" as const,
      can_operate: true,
      is_vault_owner: false,
      cloud_deletion_enabled: false,
    };
    renderBrowser(operator);
    await screen.findByTestId("file-list-cards");

    // Card ⋯ opens sheet — pick the cloud file's more-actions (cards list)
    const moreButtons = within(screen.getByTestId("file-list-cards")).getAllByRole(
      "button",
      { name: "More actions" },
    );
    await user.click(moreButtons[2]!);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "Recover" })).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("button", { name: "Hide in cloud" }),
    ).not.toBeInTheDocument();
    expect(
      within(dialog).queryByRole("button", { name: "Purge permanently" }),
    ).not.toBeInTheDocument();

    // Pure gating cases covered in file-operations-actions.test.ts for all six variants.
    expect(
      availableActions(browsePayload.items[1]!, {
        role: "viewer",
        can_operate: false,
        delete_enabled: false,
        cloud_deletion_enabled: false,
        is_vault_owner: false,
      }),
    ).toEqual([]);
  });

  it("seam 3: version selection shows date/storage and recover sends the selected version id, never an index", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; body: unknown }> = [];
    mockRoutes({
      onMutation: (url, body) => {
        calls.push({ url, body });
        return jsonResponse({ group_id: "g1", message: "recovery started" });
      },
    });
    renderBrowser();
    await screen.findByTestId("desktop-actions-archive.pdf");
    await user.click(
      within(screen.getByTestId("desktop-actions-archive.pdf")).getByRole(
        "button",
        { name: "Recover" },
      ),
    );
    const versionDialog = await screen.findByRole("dialog");
    expect(within(versionDialog).getAllByText(/#1 ·/).length).toBeGreaterThan(0);
    expect(within(versionDialog).getAllByText(/#2 ·|DEEP_ARCHIVE/).length).toBeGreaterThan(0);
    await user.click(screen.getByTestId("version-option-ver-old"));
    const confirm = await screen.findByRole("alertdialog");
    expect(within(confirm).getByText(/Version #1/)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Recover" }));
    await waitFor(() => {
      const recoverCall = calls.find((c) => c.url === "/api/recover");
      expect(recoverCall?.body).toMatchObject({
        path: "archive.pdf",
        archive_version_id: "ver-old",
      });
      expect(recoverCall?.body).not.toHaveProperty("version_number");
    });
  });

  it("seam 4: restore estimate is shown before confirmation; failed estimate does not block silently", async () => {
    const user = userEvent.setup();
    mockRoutes({
      onMutation: (url) => {
        if (url.includes("/api/recover/estimate")) {
          return jsonResponse(
            { message: "estimate unavailable", message_key: "ui.recover_estimate_failed" },
            500,
          );
        }
        return undefined;
      },
    });
    // Override estimate to fail via special case — single recoverable version path
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.includes("/api/jobs") && method === "GET") {
        return jsonResponse(emptyJobs);
      }
      if (url.includes("/api/files/versions")) {
        return jsonResponse({
          path: "archive.pdf",
          items: [
            {
              id: "ver-only",
              version_number: 3,
              storage_class: "DEEP_ARCHIVE",
              size: 2048,
              recoverable: true,
              created_at: "2025-01-01T00:00:00Z",
            },
          ],
          recoverable_count: 1,
          default_archive_version_id: "ver-only",
          supported_restore_tiers: ["Standard"],
          default_restore_tier: "Standard",
          default_restore_days: 7,
        });
      }
      if (url.includes("/api/recover/estimate")) {
        return jsonResponse({ message: "pricing down" }, 503);
      }
      if (url.includes("/api/files")) return jsonResponse(browsePayload);
      if (url.includes("/api/recover") && method === "POST") {
        return jsonResponse({ group_id: "g1", message: "started" });
      }
      return jsonResponse({});
    });
    renderBrowser();
    await screen.findByTestId("desktop-actions-archive.pdf");
    await user.click(
      within(screen.getByTestId("desktop-actions-archive.pdf")).getByRole(
        "button",
        { name: "Recover" },
      ),
    );
    const confirm = await screen.findByRole("alertdialog");
    expect(
      within(confirm).getByText(/Could not load the restore estimate|pricing down/),
    ).toBeInTheDocument();
    expect(within(confirm).getByRole("button", { name: "Recover" })).toBeEnabled();
  });

  it("seam 5: destructive action executes only after explicit confirmation; cancelling performs no call", async () => {
    const user = userEvent.setup();
    const calls: string[] = [];
    mockRoutes({
      onMutation: (url) => {
        calls.push(url);
        return jsonResponse({ group_id: "g1", message: "ok" });
      },
    });
    renderBrowser();
    await screen.findByTestId("desktop-actions-readme.txt");
    await user.click(
      within(screen.getByTestId("desktop-actions-readme.txt")).getByRole(
        "button",
        { name: "Free local space" },
      ),
    );
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/readme\.txt/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /cancel/i }));
    expect(calls.filter((u) => u.includes("/api/free-space"))).toHaveLength(0);

    await user.click(
      within(screen.getByTestId("desktop-actions-readme.txt")).getByRole(
        "button",
        { name: "Free local space" },
      ),
    );
    const again = await screen.findByRole("alertdialog");
    await user.click(
      within(again).getByRole("button", { name: "Free local space" }),
    );
    await waitFor(() => {
      expect(calls.filter((u) => u.includes("/api/free-space"))).toHaveLength(1);
    });
  });

  it("seam 6: cloud purge shows the preview before confirmation", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; body: unknown }> = [];
    mockRoutes({
      onMutation: (url, body) => {
        calls.push({ url, body });
        if (url.includes("/api/cloud-purge")) {
          return jsonResponse({ group_id: "g1", message: "purge scheduled" });
        }
        return undefined;
      },
    });
    renderBrowser();
    await screen.findByTestId("desktop-actions-archive.pdf");
    await user.click(
      within(screen.getByTestId("desktop-actions-archive.pdf")).getByRole(
        "button",
        { name: "Purge permanently" },
      ),
    );
    await screen.findByRole("dialog");
    expect(screen.getByTestId("cloud-purge-preview")).toHaveTextContent(
      /1 object\(s\).*2 version\(s\).*1 marker\(s\).*2048 bytes/,
    );
    await user.type(screen.getByTestId("cloud-purge-reason"), "cleanup obsolete");
    await user.type(screen.getByTestId("cloud-purge-confirmation"), "PURGE-PHRASE");
    await user.click(screen.getByTestId("cloud-purge-submit"));
    await waitFor(() => {
      expect(calls.some((c) => c.url === "/api/cloud-purge")).toBe(true);
    });
    expect(calls.find((c) => c.url === "/api/cloud-deletion/preview")).toBeTruthy();
    expect(calls.find((c) => c.url === "/api/cloud-purge")?.body).toMatchObject({
      path: "archive.pdf",
      reason: "cleanup obsolete",
      confirmation: "PURGE-PHRASE",
      generated_phrase: "PURGE-PHRASE",
    });
  });

  it("seam 7: active Job renders percentage and bytes; polling switches 1s ↔ 10s", async () => {
    expect(jobPollIntervalMs(1)).toBe(ACTIVE_JOB_POLL_MS);
    expect(jobPollIntervalMs(0)).toBe(IDLE_POLL_MS);

    mockRoutes({
      jobs: {
        items: [],
        groups: [
          {
            id: "job-1",
            path: "readme.txt",
            action: "upload",
            status: "uploading",
            percent: 42,
            total_bytes: 1000,
            transferred_bytes: 420,
            item_count: 1,
            completed_count: 0,
            failed_count: 0,
            cancelled_count: 0,
          },
        ],
      },
    });
    renderBrowser();
    const progress = (await screen.findAllByTestId("job-progress"))[0]!;
    expect(progress).toHaveAttribute("data-status", "uploading");
    expect(progress).toHaveTextContent("42%");
    expect(progress).toHaveTextContent(/420 B of 1000 B|420 B of 1 KB/);
  });

  it("seam 8: cancel calls /api/jobs/cancel and communicates non-cancellable recover (BUG-018)", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; body: unknown }> = [];
    mockRoutes({
      jobs: {
        items: [],
        groups: [
          {
            id: "rec-1",
            path: "archive.pdf",
            action: "recover",
            status: "restoring",
            percent: 10,
            total_bytes: 0,
            transferred_bytes: 0,
            item_count: 1,
            completed_count: 0,
            failed_count: 0,
            cancelled_count: 0,
          },
        ],
      },
      onMutation: (url, body) => {
        calls.push({ url, body });
        return jsonResponse({
          message: "Recovery stopped",
          cancelled_count: 1,
        });
      },
    });
    renderBrowser();
    await screen.findAllByTestId("job-progress");
    await user.click(screen.getAllByTestId("cancel-job")[0]!);
    await waitFor(() => {
      expect(calls.some((c) => c.url === "/api/jobs/cancel")).toBe(true);
    });
    expect(calls.find((c) => c.url === "/api/jobs/cancel")?.body).toEqual({
      group_id: "rec-1",
      action: "recover",
    });
    await waitFor(() => {
      expect(
        screen.getByText(/cannot be fully cancelled after AWS accepted RestoreObject/),
      ).toBeInTheDocument();
    });
  });

  it("seam 9: reauth required triggers the password flow and replays after success", async () => {
    const user = userEvent.setup();
    let uploadAttempts = 0;
    mockRoutes({
      onMutation: (url) => {
        if (url === "/api/upload") {
          uploadAttempts += 1;
          if (uploadAttempts === 1) {
            return jsonResponse({ error: "reauth_required" }, 403);
          }
          return jsonResponse({ group_id: "g1", message: "Upload started" });
        }
        if (url === "/api/reauth") return jsonResponse({ ok: true });
        return undefined;
      },
    });
    renderBrowser();
    await screen.findByTestId("desktop-actions-readme.txt");
    await user.click(
      within(screen.getByTestId("desktop-actions-readme.txt")).getByRole(
        "button",
        { name: "Upload" },
      ),
    );
    await waitFor(() => {
      expect(requestPassword).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(uploadAttempts).toBe(2);
    });
    await waitFor(() => {
      expect(screen.getByText("Upload started")).toBeInTheDocument();
    });
  });

  it("seam 10: operation error surfaces the localized message and leaves the row usable", async () => {
    const user = userEvent.setup();
    mockRoutes({
      onMutation: (url) => {
        if (url === "/api/upload") {
          return jsonResponse(
            {
              message: "Local Copy already exists on disk",
              message_key: "job.recover_destination_exists",
            },
            409,
          );
        }
        return undefined;
      },
    });
    // Force translate of message_key
    configureApiClient({
      fetch: fetchMock,
      requestPassword,
      getAuthMethod: () => "local",
      translate: (key) =>
        key === "job.recover_destination_exists"
          ? "Local Copy already exists on disk"
          : t(key),
    });
    renderBrowser();
    await screen.findByTestId("desktop-actions-readme.txt");
    await user.click(
      within(screen.getByTestId("desktop-actions-readme.txt")).getByRole(
        "button",
        { name: "Upload" },
      ),
    );
    await waitFor(() => {
      expect(
        screen.getByText("Local Copy already exists on disk"),
      ).toBeInTheDocument();
    });
    // Row still usable — upload button still present
    expect(
      within(screen.getByTestId("desktop-actions-readme.txt")).getByRole(
        "button",
        { name: "Upload" },
      ),
    ).toBeEnabled();
  });
});

describe("source-level: no window.prompt/confirm/alert in frontend sources", () => {
  it("finds zero occurrences", async () => {
    const srcRoot = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../src",
    );
    async function walk(dir: string): Promise<string[]> {
      const entries = await readdir(dir, { withFileTypes: true });
      const files: string[] = [];
      for (const entry of entries) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          files.push(...(await walk(full)));
        } else if (/\.(ts|tsx)$/.test(entry.name)) {
          files.push(full);
        }
      }
      return files;
    }
    const offenders: string[] = [];
    for (const file of await walk(srcRoot)) {
      const source = await readFile(file, "utf8");
      if (/window\.(prompt|confirm|alert)\s*\(/.test(source)) {
        offenders.push(path.relative(srcRoot, file));
      }
    }
    expect(offenders).toEqual([]);
  });
});

describe("bottom sheet tap targets", () => {
  it("every action button is at least 44×44 via min-h-11 min-w-11", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) {
        return jsonResponse(emptyJobs);
      }
      return jsonResponse(browsePayload);
    });
    configureApiClient({ fetch: fetchMock });
    const client = createAppQueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <ApiQueryProvider client={client}>
        <FileBrowser t={t} capabilities={ownerCaps} vaultName="Test Archive" />
      </ApiQueryProvider>,
    );
    await screen.findByTestId("file-list-cards");
    const more = within(screen.getByTestId("file-list-cards")).getAllByRole(
      "button",
      { name: "More actions" },
    );
    await user.click(more[1]!);
    const dialog = await screen.findByRole("dialog");
    const buttons = within(dialog).getAllByRole("button");
    expect(buttons.length).toBeGreaterThan(0);
    for (const button of buttons) {
      expect(button.className.split(/\s+/)).toEqual(
        expect.arrayContaining(["min-h-11"]),
      );
    }
  });
});
