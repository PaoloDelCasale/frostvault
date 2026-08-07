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
  "ui.row_action_upload": "Upload",
  "ui.row_action_recover": "Recover",
  "ui.row_action_free_space": "Free local space",
  "ui.row_action_cloud_archive": "Hide in cloud",
  "ui.row_action_cloud_purge": "Purge permanently",
  "ui.action_directory_count": "{action} {count} files",
  "ui.row_actions_title": "Actions for {name}",
  "ui.cancel": "Cancel",
  "ui.path_history": "Path History",
  "ui.path_history_versions": "{count} Archive Versions",
  "ui.path_history_no_versions": "No Archive Versions",
  "ui.path_history_loading": "Loading Path History…",
  "ui.path_history_error": "Unable to load Path History.",
  "ui.close_path_history": "Close",
  "ui.file_list_placeholder": "File list",
  "ui.file_list_loading": "Loading folder…",
  "ui.file_list_error": "Unable to load this folder.",
  "ui.file_list_retry": "Retry",
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


const testCapabilities = {
  role: "owner" as const,
  can_operate: true,
  delete_enabled: true,
  cloud_deletion_enabled: false,
  is_vault_owner: true,
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
        <FileBrowser t={t} capabilities={testCapabilities} vaultId={1} vaultName="Test Archive" />
      </ApiQueryProvider>,
    );
  }

  it("renders a realistic /api/files payload as cards below md and as a table from md up, with the same information in both", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) {
        return jsonResponse({ items: [], groups: [] });
      }
      return jsonResponse(realisticBrowse);
    });
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

    // Actions: ⋯ on cards (bottom sheet in #67); inline buttons on the table
    expect(within(cards).getAllByRole("button", { name: "More actions" })).toHaveLength(3);
    expect(within(table).queryAllByRole("button", { name: "More actions" })).toHaveLength(0);
    expect(
      within(table).getByTestId("desktop-actions-readme.txt"),
    ).toBeInTheDocument();
    expect(
      within(table).getByRole("button", { name: "Free local space" }),
    ).toBeInTheDocument();
    expect(
      within(table).getByRole("button", { name: "Recover" }),
    ).toBeInTheDocument();

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/^\/api\/files\?/),
      expect.anything(),
    );
  });
});

describe("FileBrowser — directory navigation", () => {
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
        <FileBrowser t={t} capabilities={testCapabilities} vaultId={1} vaultName="Test Archive" />
      </ApiQueryProvider>,
    );
  }

  function filesUrlDirectory(callIndex?: number): string {
    const calls = fetchMock.mock.calls
      .map((call) => String(call[0] ?? ""))
      .filter((url) => url.includes("/api/files"));
    const url =
      callIndex == null
        ? (calls.at(-1) ?? "")
        : String(fetchMock.mock.calls[callIndex]?.[0] ?? "");
    return new URL(url, "http://localhost").searchParams.get("directory") || "";
  }

  it("navigates into a directory: the request carries the new directory and the URL is updated", async () => {
    const { userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();

    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const raw = String(input);
      if (raw.includes("/api/jobs")) {
        return jsonResponse({ items: [], groups: [] });
      }
      const url = new URL(raw, "http://localhost");
      const directory = url.searchParams.get("directory") || "";
      if (directory === "reports") {
        return jsonResponse({
          items: [
            {
              type: "file",
              name: "q1.pdf",
              path: "reports/q1.pdf",
              local_size: 512,
              cloud_size: 512,
              state: "both",
              storage_class: "STANDARD",
              cloud_exists: 1,
              local_exists: 1,
            },
          ],
          total: 1,
          page: 1,
          directory: "reports",
          mode: "browse",
        });
      }
      return jsonResponse(realisticBrowse);
    });

    renderBrowser();
    await waitFor(() => {
      expect(screen.getByTestId("file-list-cards")).toBeInTheDocument();
    });

    const folderButtons = screen.getAllByRole("button", { name: /reports/i });
    await user.click(folderButtons[0]!);

    await waitFor(() => {
      expect(filesUrlDirectory()).toBe("reports");
    });
    expect(new URLSearchParams(window.location.search).get("directory")).toBe(
      "reports",
    );
    await waitFor(() => {
      expect(screen.getAllByText("q1.pdf").length).toBeGreaterThan(0);
    });
  });

  it("navigates via Up and breadcrumbs, and the browser back button returns to the previous directory", async () => {
    const { userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();

    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) return jsonResponse({ items: [], groups: [] });
      const url = new URL(String(input), "http://localhost");
      const directory = url.searchParams.get("directory") || "";
      if (directory === "reports/2024") {
        return jsonResponse({
          items: [],
          total: 0,
          page: 1,
          directory: "reports/2024",
          mode: "browse",
        });
      }
      if (directory === "reports") {
        return jsonResponse({
          items: [
            {
              type: "directory",
              name: "2024",
              path: "reports/2024",
              item_count: 1,
              total_size: 100,
              state: "both",
              storage_class: "STANDARD",
              storage_class_count: 1,
            },
          ],
          total: 1,
          page: 1,
          directory: "reports",
          mode: "browse",
        });
      }
      return jsonResponse(realisticBrowse);
    });

    // Start already nested so breadcrumbs have ancestors.
    window.history.replaceState(
      { directory: "reports/2024", q: "", state: "", page: 1 },
      "",
      "/?directory=reports%2F2024",
    );
    renderBrowser();

    await waitFor(() => {
      expect(screen.getByTestId("up-directory")).not.toBeDisabled();
    });

    // Breadcrumb to "reports"
    const reportsCrumbs = screen
      .getAllByRole("button")
      .filter((el) => el.getAttribute("data-directory") === "reports");
    expect(reportsCrumbs.length).toBeGreaterThan(0);
    await user.click(reportsCrumbs[0]!);

    await waitFor(() => {
      expect(new URLSearchParams(window.location.search).get("directory")).toBe(
        "reports",
      );
    });

    // Up to archive root
    await user.click(screen.getByTestId("up-directory"));
    await waitFor(() => {
      expect(
        new URLSearchParams(window.location.search).get("directory"),
      ).toBeNull();
    });

    // Push into reports again, then use browser back
    const folderButtons = screen.getAllByRole("button", { name: /reports/i });
    await user.click(folderButtons[0]!);
    await waitFor(() => {
      expect(new URLSearchParams(window.location.search).get("directory")).toBe(
        "reports",
      );
    });

    window.history.back();
    await waitFor(() => {
      expect(
        new URLSearchParams(window.location.search).get("directory"),
      ).toBeNull();
    });
    // Root listing should reload
    await waitFor(() => {
      expect(screen.getAllByText("readme.txt").length).toBeGreaterThan(0);
    });
  });

  it("collapses deep breadcrumbs on the narrow trail instead of overflowing", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) {
        return jsonResponse({ items: [], groups: [] });
      }
      return jsonResponse({
        items: [],
        total: 0,
        page: 1,
        directory: "a/b/c/d/e/f",
        mode: "browse",
      });
    });
    window.history.replaceState(
      {},
      "",
      "/?directory=a%2Fb%2Fc%2Fd%2Fe%2Ff",
    );
    renderBrowser();

    await waitFor(() => {
      expect(screen.getByTestId("breadcrumbs-narrow")).toBeInTheDocument();
    });

    const narrow = screen.getByTestId("breadcrumbs-narrow");
    expect(narrow.className.split(/\s+/)).toEqual(
      expect.arrayContaining(["md:hidden"]),
    );
    // Ellipsis present; intermediate segments like "b" and "c" omitted
    expect(within(narrow).getByText("…")).toBeInTheDocument();
    expect(within(narrow).queryByText("b")).not.toBeInTheDocument();
    expect(within(narrow).queryByText("c")).not.toBeInTheDocument();
    // Root and last segments remain
    expect(within(narrow).getByText("Archive")).toBeInTheDocument();
    expect(within(narrow).getByText("f")).toBeInTheDocument();
    // No overflow scroll class — trail wraps/truncates
    expect(narrow.className).not.toMatch(/overflow-x-auto/);
  });
});

describe("FileBrowser — search, filter, pagination", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    configureApiClient({ fetch: fetchMock });
    window.history.replaceState({}, "", "/");
    vi.useFakeTimers({ shouldAdvanceTime: true });
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
        <FileBrowser t={t} capabilities={testCapabilities} vaultId={1} vaultName="Test Archive" />
      </ApiQueryProvider>,
    );
  }

  function lastFilesParams(): URLSearchParams {
    const url =
      fetchMock.mock.calls
        .map((call) => String(call[0] ?? ""))
        .filter((value) => value.includes("/api/files"))
        .at(-1) ?? "";
    return new URL(url, "http://localhost").searchParams;
  }

  it("debounces search typing and issues a single request with the right q", async () => {
    const { userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) {
        return jsonResponse({ items: [], groups: [] });
      }
      return jsonResponse({
        items: [],
        total: 0,
        page: 1,
        directory: "",
        mode: "search",
      });
    });

    renderBrowser();
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    const callsBeforeTyping = fetchMock.mock.calls.length;

    await user.type(screen.getByTestId("file-search"), "lease");
    // Before debounce fires, no extra request
    expect(fetchMock.mock.calls.length).toBe(callsBeforeTyping);

    await vi.advanceTimersByTimeAsync(250);

    await waitFor(() => {
      expect(lastFilesParams().get("q")).toBe("lease");
    });
    // One debounced search request (not one per keystroke)
    const searchCalls = fetchMock.mock.calls.filter((call) =>
      String(call[0]).includes("q=lease"),
    );
    expect(searchCalls).toHaveLength(1);
    expect(new URLSearchParams(window.location.search).get("q")).toBe("lease");
  });

  it("passes the selected state filter and keeps it when combined with search", async () => {
    const { userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) {
        return jsonResponse({ items: [], groups: [] });
      }
      return jsonResponse({
        items: [],
        total: 0,
        page: 1,
        directory: "",
        mode: "browse",
      });
    });

    renderBrowser();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    await user.selectOptions(screen.getByTestId("state-filter"), "cloud_only");
    await waitFor(() => {
      expect(lastFilesParams().get("state")).toBe("cloud_only");
    });
    expect(new URLSearchParams(window.location.search).get("state")).toBe(
      "cloud_only",
    );

    await user.type(screen.getByTestId("file-search"), "pdf");
    await vi.advanceTimersByTimeAsync(250);

    await waitFor(() => {
      expect(lastFilesParams().get("q")).toBe("pdf");
    });
    expect(lastFilesParams().get("state")).toBe("cloud_only");
    const params = new URLSearchParams(window.location.search);
    expect(params.get("q")).toBe("pdf");
    expect(params.get("state")).toBe("cloud_only");
  });

  it("requests the right page and disables pagination controls at the boundaries", async () => {
    const { userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) return jsonResponse({ items: [], groups: [] });
      const page = Number(
        new URL(String(input), "http://localhost").searchParams.get("page") ||
          "1",
      );
      return jsonResponse({
        items: [
          {
            type: "file",
            name: `file-p${page}.txt`,
            path: `file-p${page}.txt`,
            local_size: 10,
            state: "local_only",
            cloud_exists: 0,
            local_exists: 1,
          },
        ],
        total: 250,
        page,
        directory: "",
        mode: "browse",
      });
    });

    renderBrowser();
    await waitFor(() => {
      expect(screen.getByTestId("page-previous")).toBeDisabled();
    });
    expect(screen.getByTestId("page-next")).not.toBeDisabled();
    expect(lastFilesParams().get("page")).toBe("1");

    await user.click(screen.getByTestId("page-next"));
    await waitFor(() => {
      expect(lastFilesParams().get("page")).toBe("2");
    });
    expect(screen.getByTestId("page-previous")).not.toBeDisabled();
    expect(screen.getByTestId("page-next")).not.toBeDisabled();

    await user.click(screen.getByTestId("page-next"));
    await waitFor(() => {
      expect(lastFilesParams().get("page")).toBe("3");
    });
    // 250 items / 100 = 3 pages → next disabled on last page
    expect(screen.getByTestId("page-next")).toBeDisabled();
    expect(screen.getByTestId("page-previous")).not.toBeDisabled();
  });
});

describe("FileBrowser — Path History, empty states, HTML safety", () => {
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
        <FileBrowser t={t} capabilities={testCapabilities} vaultId={1} vaultName="Test Archive" />
      </ApiQueryProvider>,
    );
  }

  it("loads and displays Path History when a Vault File is tapped", async () => {
    const { userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();

    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) return jsonResponse({ items: [], groups: [] });
      const url = String(input);
      if (url.startsWith("/api/file-history")) {
        return jsonResponse({
          vault_file_id: "vf-1",
          path: "readme.txt",
          path_history: [
            { path: "docs/old-readme.txt", valid_from: "2024-01-01" },
            { path: "readme.txt", valid_from: "2024-06-01" },
          ],
          versions: [
            { object_key: "bucket/readme.txt" },
            { object_key: "bucket/docs/old-readme.txt" },
          ],
        });
      }
      return jsonResponse(realisticBrowse);
    });

    renderBrowser();
    await waitFor(() => {
      expect(screen.getAllByText("readme.txt").length).toBeGreaterThan(0);
    });

    const fileButtons = screen
      .getAllByRole("button")
      .filter((el) => el.getAttribute("data-file-path") === "readme.txt");
    await user.click(fileButtons[0]!);

    await waitFor(() => {
      expect(screen.getByTestId("path-history")).toBeInTheDocument();
    });
    expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/api/file-history"))).toBe(
      true,
    );
    const timeline = screen.getByTestId("path-history-timeline");
    expect(within(timeline).getByText("docs/old-readme.txt")).toBeInTheDocument();
    expect(within(timeline).getByText("readme.txt")).toBeInTheDocument();
    expect(screen.getByTestId("path-history-versions")).toHaveTextContent(
      "2 Archive Versions",
    );
  });

  it("renders distinguishable empty states for no files vs no matches", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) {
        return jsonResponse({ items: [], groups: [] });
      }
      return jsonResponse({
        items: [],
        total: 0,
        page: 1,
        directory: "",
        mode: "browse",
      });
    });

    const { rerender } = (() => {
      const client = createAppQueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      const view = render(
        <ApiQueryProvider client={client}>
          <FileBrowser t={t} capabilities={testCapabilities} vaultId={1} vaultName="Test Archive" />
        </ApiQueryProvider>,
      );
      return view;
    })();
    void rerender;

    await waitFor(() => {
      expect(screen.getByTestId("file-list-empty")).toHaveAttribute(
        "data-empty",
        "no-files",
      );
    });
    expect(screen.getByTestId("file-list-empty")).toHaveTextContent(
      "This folder is empty.",
    );

    cleanup();
    window.history.replaceState({}, "", "/?q=zzzz");
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) {
        return jsonResponse({ items: [], groups: [] });
      }
      return jsonResponse({
        items: [],
        total: 0,
        page: 1,
        directory: "",
        mode: "search",
      });
    });
    renderBrowser();

    await waitFor(() => {
      expect(screen.getByTestId("file-list-empty")).toHaveAttribute(
        "data-empty",
        "no-matches",
      );
    });
    expect(screen.getByTestId("file-list-empty")).toHaveTextContent(
      "No Vault Files match your search.",
    );
  });

  it("shows a loading skeleton instead of a false-empty 0 items label", async () => {
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) {
        return jsonResponse({ items: [], groups: [] });
      }
      await gate;
      return jsonResponse(realisticBrowse);
    });

    renderBrowser();
    expect(screen.getByTestId("file-list-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("file-list-empty")).not.toBeInTheDocument();
    expect(screen.getByTestId("page-label")).toHaveTextContent("Loading folder…");
    expect(screen.getByTestId("page-label")).not.toHaveTextContent("0 items");

    release?.();
    await waitFor(() => {
      expect(screen.getByTestId("file-list-cards")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("file-list-loading")).not.toBeInTheDocument();
    expect(screen.getByTestId("page-label")).toHaveTextContent("3 items");
  });

  it("treats aggregate_status=loading with empty items as loading, not empty", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) {
        return jsonResponse({ items: [], groups: [] });
      }
      return jsonResponse({
        items: [],
        total: 0,
        page: 1,
        directory: "",
        mode: "browse",
        aggregate_status: "loading",
      });
    });

    renderBrowser();
    await waitFor(() => {
      expect(screen.getByTestId("file-list-loading")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("file-list-empty")).not.toBeInTheDocument();
    expect(screen.getByTestId("page-label")).toHaveTextContent("Loading folder…");
    expect(screen.getByTestId("page-label")).not.toHaveTextContent("0 items");
  });

  it("exposes an accessible retry control when the listing fails", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) {
        return jsonResponse({ items: [], groups: [] });
      }
      return new Response("boom", { status: 500 });
    });

    renderBrowser();
    await waitFor(() => {
      expect(screen.getByTestId("file-list-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("file-list-retry")).toHaveTextContent("Retry");
    expect(screen.queryByTestId("file-list-empty")).not.toBeInTheDocument();
  });

  it("renders a file name containing HTML as text, not as markup", async () => {
    const evilName = "<img src=x onerror=alert(1)>";
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/jobs")) {
        return jsonResponse({ items: [], groups: [] });
      }
      return jsonResponse({
        items: [
          {
            type: "file",
            name: evilName,
            path: evilName,
            local_size: 1,
            state: "local_only",
            cloud_exists: 0,
            local_exists: 1,
          },
        ],
        total: 1,
        page: 1,
        directory: "",
        mode: "browse",
      });
    });

    renderBrowser();
    await waitFor(() => {
      expect(screen.getAllByText(evilName).length).toBeGreaterThan(0);
    });
    // React text nodes — never interpreted as HTML elements
    expect(document.querySelector("img")).toBeNull();
    const nameNodes = screen.getAllByText(evilName);
    for (const node of nameNodes) {
      expect(node.tagName).toBe("SPAN");
      expect(node.textContent).toBe(evilName);
      // Serialized child HTML escapes the angle brackets
      expect(node.innerHTML).toBe(
        "&lt;img src=x onerror=alert(1)&gt;",
      );
    }
  });
});
