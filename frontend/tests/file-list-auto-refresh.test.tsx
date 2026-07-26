import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ACTIVE_JOB_POLL_MS,
  ApiQueryProvider,
  IDLE_POLL_MS,
  configureApiClient,
  countActiveJobGroups,
  createAppQueryClient,
  filesRefetchIntervalFromJobs,
  resetApiClientForTests,
} from "@/api";
import type { FilesResponse, JobsResponse, MeVault } from "@/api/types";
import { FileBrowser } from "@/pages/archive/FileBrowser";

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
  "ui.cancel": "Cancel",
  "ui.stop": "Stop",
  "ui.stopping": "Stopping…",
  "ui.job_bytes_progress": "{transferred} of {total}",
  "ui.job_files_progress": "{completed} of {total} files",
  "operation.uploading": "Uploading",
  "operation.queued": "Waiting",
  "operation.completed": "Completed",
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

const localOnlyFile: FilesResponse = {
  items: [
    {
      type: "file",
      name: "readme.txt",
      path: "readme.txt",
      local_exists: 1,
      local_size: 1024,
      cloud_exists: 0,
      state: "local_only",
      upload_eligible: true,
      recover_eligible: false,
      cleanup_eligible: false,
    },
  ],
  total: 1,
  page: 1,
  directory: "",
  mode: "browse",
};

const bothFile: FilesResponse = {
  items: [
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
      upload_eligible: false,
      recover_eligible: false,
      cleanup_eligible: true,
    },
  ],
  total: 1,
  page: 1,
  directory: "",
  mode: "browse",
};

const activeUploadJob: JobsResponse = {
  items: [],
  groups: [
    {
      id: "job-1",
      path: "readme.txt",
      action: "upload",
      status: "uploading",
      percent: 40,
      total_bytes: 1000,
      transferred_bytes: 400,
      item_count: 1,
      completed_count: 0,
      failed_count: 0,
      cancelled_count: 0,
    },
  ],
};

const completedUploadJob: JobsResponse = {
  items: [],
  groups: [
    {
      id: "job-1",
      path: "readme.txt",
      action: "upload",
      status: "completed",
      percent: 100,
      total_bytes: 1000,
      transferred_bytes: 1000,
      item_count: 1,
      completed_count: 1,
      failed_count: 0,
      cancelled_count: 0,
    },
  ],
};

describe("filesRefetchIntervalFromJobs", () => {
  it("uses the same 1s ↔ 10s cadence as jobs/stats from active Job groups", () => {
    expect(filesRefetchIntervalFromJobs(undefined)).toBe(IDLE_POLL_MS);
    expect(filesRefetchIntervalFromJobs({ items: [], groups: [] })).toBe(
      IDLE_POLL_MS,
    );
    expect(countActiveJobGroups(activeUploadJob)).toBe(1);
    expect(filesRefetchIntervalFromJobs(activeUploadJob)).toBe(
      ACTIVE_JOB_POLL_MS,
    );
    expect(countActiveJobGroups(completedUploadJob)).toBe(0);
    expect(filesRefetchIntervalFromJobs(completedUploadJob)).toBe(IDLE_POLL_MS);
  });
});

describe("File list auto-refresh (issue #128)", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    configureApiClient({
      fetch: fetchMock,
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

  function renderBrowser() {
    const client = createAppQueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <ApiQueryProvider client={client}>
        <FileBrowser t={t} capabilities={ownerCaps} vaultName="Test Archive" />
      </ApiQueryProvider>,
    );
  }

  it("seam 1: polls /api/files every 1s while Jobs are active and refreshes row state", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let filesCalls = 0;

    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url,
      );
      if (url.includes("/api/jobs")) {
        return jsonResponse(activeUploadJob);
      }
      if (url.includes("/api/files")) {
        filesCalls += 1;
        return jsonResponse(filesCalls === 1 ? localOnlyFile : bothFile);
      }
      return jsonResponse({});
    });

    renderBrowser();

    await waitFor(() => {
      expect(screen.getAllByText("Server only").length).toBeGreaterThan(0);
    });
    expect(filesCalls).toBe(1);

    await vi.advanceTimersByTimeAsync(ACTIVE_JOB_POLL_MS);

    await waitFor(() => {
      expect(filesCalls).toBeGreaterThanOrEqual(2);
      expect(screen.getAllByText("Server + cloud").length).toBeGreaterThan(0);
    });
  });

  it("seam 2: when a Job becomes terminal, the file list refetches and badges update", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let releaseCompletion = false;
    let jobsCalls = 0;
    let filesCalls = 0;

    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url,
      );
      if (url.includes("/api/jobs")) {
        jobsCalls += 1;
        return jsonResponse(
          releaseCompletion ? completedUploadJob : activeUploadJob,
        );
      }
      if (url.includes("/api/files")) {
        filesCalls += 1;
        return jsonResponse(
          releaseCompletion && filesCalls > 1 ? bothFile : localOnlyFile,
        );
      }
      return jsonResponse({});
    });

    renderBrowser();

    await waitFor(() => {
      expect(screen.getAllByText("Server only").length).toBeGreaterThan(0);
      expect(screen.getAllByTestId("job-progress").length).toBeGreaterThan(0);
    });

    // Job finishes on the next jobs poll; active-count drop invalidates files.
    releaseCompletion = true;
    await vi.advanceTimersByTimeAsync(ACTIVE_JOB_POLL_MS);

    await waitFor(() => {
      expect(jobsCalls).toBeGreaterThanOrEqual(2);
      expect(filesCalls).toBeGreaterThanOrEqual(2);
      expect(screen.getAllByText("Server + cloud").length).toBeGreaterThan(0);
      expect(screen.queryByTestId("job-progress")).not.toBeInTheDocument();
    });
  });

  it("seam 3: cancel recover keeps restoring badge when catalog still reports restoring (BUG-018)", async () => {
    const restoringFile: FilesResponse = {
      items: [
        {
          type: "file",
          name: "archive.pdf",
          path: "archive.pdf",
          local_exists: 0,
          cloud_exists: 1,
          cloud_size: 2048,
          storage_class: "DEEP_ARCHIVE",
          state: "restoring",
          restore_state: "restoring",
          upload_eligible: false,
          recover_eligible: false,
          cleanup_eligible: false,
        },
      ],
      total: 1,
      page: 1,
      directory: "",
      mode: "browse",
    };
    const activeRecover: JobsResponse = {
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
    };
    let jobs: JobsResponse = activeRecover;
    let filesCalls = 0;

    fetchMock.mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url,
        );
        const method = (init?.method ?? "GET").toUpperCase();
        if (url.includes("/api/jobs") && method === "GET") {
          return jsonResponse(jobs);
        }
        if (url.includes("/api/jobs/cancel") && method === "POST") {
          jobs = { items: [], groups: [] };
          return jsonResponse({
            message: "Recovery stopped",
            cancelled_count: 1,
          });
        }
        if (url.includes("/api/files")) {
          filesCalls += 1;
          return jsonResponse(restoringFile);
        }
        return jsonResponse({});
      },
    );

    const user = userEvent.setup();
    renderBrowser();

    await waitFor(() => {
      expect(
        screen.getAllByText("Recovery in progress").length,
      ).toBeGreaterThan(0);
      expect(screen.getAllByTestId("job-progress").length).toBeGreaterThan(0);
    });

    await user.click(screen.getAllByTestId("cancel-job")[0]!);

    await waitFor(() => {
      expect(screen.queryByTestId("job-progress")).not.toBeInTheDocument();
      expect(
        screen.getAllByText("Recovery in progress").length,
      ).toBeGreaterThan(0);
      expect(filesCalls).toBeGreaterThanOrEqual(2);
    });
  });
});
