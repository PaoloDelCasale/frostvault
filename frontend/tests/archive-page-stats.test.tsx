import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import type { StatsResponse } from "@/api/types";
import { ArchivePage } from "@/pages/archive/ArchivePage";

const messages: Record<string, string> = {
  "ui.archive_subtitle":
    "Your files, on the server and safely stored in the cloud.",
  "ui.archive_statistics": "Archive statistics",
  "ui.filesystem_needs_attention": "Vault filesystem needs attention",
  "ui.filesystem_attention_detail":
    "Symbolic links and permission errors are reported; ownership and modes are never changed automatically.",
  "ui.file_list_placeholder": "File list",
  "ui.server_space": "Server space",
  "ui.cloud_space": "Cloud space",
  "ui.active_operations": "Active operations",
  "state.both": "Server + cloud",
  "state.local_only": "Server only",
  "state.cloud_only": "Cloud only",
  "ui.protected_archive": "Protected archive · {name}",
  "ui.protected_archive_detail":
    "Local cleanup is allowed only after the S3 copy has been verified.",
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

/** Empty current Vault — matches an empty catalog from /api/files. */
const emptyVaultStats: StatsResponse = {
  states: { both: 0, local_only: 0, cloud_only: 0 },
  storage: { local_bytes: 0, cloud_bytes: 0 },
  active_jobs: 0,
  runtime: {},
  filesystem: {
    ok: true,
    uid: 1000,
    gid: 1000,
    root: "/sources/new-vault",
    checks: [],
    findings: [],
  },
  delete_enabled: false,
};

/** Non-demo live values — must not collide with hardcoded demoStats (12 / 7 / 100 MB / alias.txt). */
const liveVaultStats: StatsResponse = {
  states: { both: 4, local_only: 1, cloud_only: 2 },
  storage: { local_bytes: 512, cloud_bytes: 2048 },
  active_jobs: 0,
  runtime: {},
  filesystem: {
    ok: true,
    uid: 1000,
    gid: 1000,
    root: "/sources/live",
    checks: [],
    findings: [],
  },
  delete_enabled: true,
};

describe("ArchivePage from /api/stats", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    configureApiClient({ fetch: fetchMock });
  });

  afterEach(() => {
    cleanup();
    resetApiClientForTests();
  });

  function renderArchive() {
    const client = createAppQueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <ApiQueryProvider client={client}>
        <ArchivePage
          vaultName="New Archive"
          displayName="Operator"
          t={t}
        />
      </ApiQueryProvider>,
    );
  }

  it("shows zeros and no filesystem alarm for an empty Vault /api/stats payload", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      expect(String(input)).toContain("/api/stats");
      return jsonResponse(emptyVaultStats);
    });

    renderArchive();

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });

    await waitFor(() => {
      // Compact + expanded both render the same zeros.
      expect(screen.getAllByText("0").length).toBeGreaterThanOrEqual(6);
    });

    expect(screen.getAllByText("0 B").length).toBeGreaterThan(0);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/alias\.txt/i)).not.toBeInTheDocument();
    // Hardcoded demoStats must not leak into an empty Vault shell.
    expect(screen.queryByText("12")).not.toBeInTheDocument();
    expect(screen.queryByText("100.0 MB")).not.toBeInTheDocument();
  });

  it("renders the live /api/stats values in the archive shell", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      expect(String(input)).toContain("/api/stats");
      return jsonResponse(liveVaultStats);
    });

    renderArchive();

    await waitFor(() => {
      expect(screen.getAllByText("4").length).toBeGreaterThan(0);
    });

    expect(screen.getAllByText("1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("2").length).toBeGreaterThan(0);
    expect(screen.getAllByText("512 B").length).toBeGreaterThan(0);
    expect(screen.getAllByText("2.0 KB").length).toBeGreaterThan(0);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/alias\.txt/i)).not.toBeInTheDocument();
  });
});
