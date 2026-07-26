import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import type { FilesResponse } from "@/api/types";
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

/** Realistic browse payload matching /api/files directory listing shape. */
const realisticBrowse: FilesResponse = {
  items: [
    {
      type: "directory",
      name: "reports",
      path: "reports",
      item_count: 3,
      total_size: 1536,
      local_size: 1024,
      cloud_size: 512,
      state: "mixed",
      state_counts: { both: 2, local_only: 1 },
      storage_class: null,
      storage_class_count: 2,
      available_actions: { upload: 1, recover: 0, "free-space": 2 },
    },
    {
      type: "file",
      name: "readme.txt",
      path: "readme.txt",
      local_exists: 1,
      local_size: 1024,
      local_file_type: "regular",
      cloud_exists: 1,
      cloud_size: 1024,
      storage_class: "STANDARD",
      state: "both",
      upload_eligible: false,
      recover_eligible: false,
      cleanup_eligible: true,
      recoverable_version_count: 1,
    },
    {
      type: "file",
      name: "archive.pdf",
      path: "archive.pdf",
      local_exists: 0,
      local_size: null,
      local_file_type: null,
      cloud_exists: 1,
      cloud_size: 2048,
      storage_class: "DEEP_ARCHIVE",
      state: "cloud_only",
      upload_eligible: false,
      recover_eligible: true,
      cleanup_eligible: false,
      recoverable_version_count: 1,
    },
  ],
  total: 3,
  page: 1,
  directory: "",
  mode: "browse",
};

describe("FileBrowser — cards and table from /api/files", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    configureApiClient({ fetch: fetchMock });
    window.history.replaceState({}, "", "/");
  });

  afterEach(() => {
    cleanup();
    resetApiClientForTests();
  });

  function renderBrowser() {
    const client = createAppQueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <ApiQueryProvider client={client}>
        <FileBrowser t={t} />
      </ApiQueryProvider>,
    );
  }

  it("renders a realistic /api/files payload as cards below md and as a table from md up, with the same information in both", async () => {
    fetchMock.mockResolvedValue(jsonResponse(realisticBrowse));
    renderBrowser();

    await waitFor(() => {
      expect(screen.getByTestId("file-list-cards")).toBeInTheDocument();
    });

    const cards = screen.getByTestId("file-list-cards");
    const table = screen.getByTestId("file-list-table");

    expect(cards.className.split(/\s+/)).toEqual(
      expect.arrayContaining(["md:hidden"]),
    );
    expect(table.className.split(/\s+/)).toEqual(
      expect.arrayContaining(["hidden", "md:block"]),
    );

    // Same Vault File / folder names in both renderings
    for (const name of ["reports", "readme.txt", "archive.pdf"]) {
      expect(within(cards).getAllByText(name).length).toBeGreaterThan(0);
      expect(within(table).getAllByText(name).length).toBeGreaterThan(0);
    }

    // Size, state Badge, and cloud storage class appear in both
    expect(within(cards).getAllByText("1.5 KB").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("1.5 KB").length).toBeGreaterThan(0);
    expect(within(cards).getAllByText("Mixed state").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Mixed state").length).toBeGreaterThan(0);
    expect(within(cards).getAllByText("Server + cloud").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Server + cloud").length).toBeGreaterThan(0);
    expect(within(cards).getAllByText("Deep Archive").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("Deep Archive").length).toBeGreaterThan(0);
    expect(within(cards).getAllByText("2 cloud classes").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("2 cloud classes").length).toBeGreaterThan(0);

    // Actions column home: ⋯ more-actions control present per row in both
    expect(within(cards).getAllByRole("button", { name: "More actions" })).toHaveLength(3);
    expect(within(table).getAllByRole("button", { name: "More actions" })).toHaveLength(3);

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/^\/api\/files\?/),
      expect.anything(),
    );
  });
});
