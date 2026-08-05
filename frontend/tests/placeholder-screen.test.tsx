import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import App from "@/App";
import { I18nProvider } from "@/i18n/I18nProvider";

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("App shell screen", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    configureApiClient({ fetch: fetchMock });
    window.history.replaceState({}, "", "/");
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/me")) {
        return jsonResponse({
          id: 1,
          username: "owner",
          display_name: "Owner",
          is_admin: true,
          active: true,
          session_version: 1,
          csrf_token: "csrf",
          offline_cache_generation: "owner-session-vault-1",
          auth_method: "local",
          locale: "en",
          locales: ["en", "it"],
          vault: {
            id: 1,
            slug: "test",
            name: "Test Archive",
            role: "owner",
            can_operate: true,
            delete_enabled: true,
            cloud_deletion_enabled: true,
            is_vault_owner: true,
          },
        });
      }
      if (url.includes("/api/vaults")) {
        return jsonResponse({
          items: [
            { id: 1, slug: "test", name: "Test Archive", role: "owner" },
          ],
        });
      }
      if (url.includes("/api/i18n/catalog")) {
        return jsonResponse({
          locale: "en",
          locales: ["en", "it"],
          messages: {
            "ui.archive_subtitle":
              "Your files, on the server and safely stored in the cloud.",
            "ui.search_placeholder": "Search by file or folder name…",
            "ui.filter_by_state": "Filter by state",
            "ui.all_items": "All items",
            "ui.empty_no_files": "This folder is empty.",
            "ui.go_up": "Go to parent folder",
            "ui.up": "↑ Up",
            "ui.breadcrumb_archive": "Archive",
            "ui.name": "Name",
            "ui.size": "Size",
            "ui.state": "State",
            "ui.cloud_storage": "Cloud storage",
            "ui.more_actions": "More actions",
            "ui.file_total": "File total",
            "ui.items_unit": "items",
            "ui.page_label": "Page {page} of {pages} · {total} {unit}",
            "ui.previous": "Previous",
            "ui.next": "Next",
            "ui.server_space": "Server space",
            "ui.cloud_space": "Cloud space",
            "ui.active_operations": "Active operations",
            "ui.archive_statistics": "Archive statistics",
            "ui.filesystem_needs_attention": "Vault filesystem needs attention",
            "ui.filesystem_attention_detail": "detail",
            "ui.protected_archive": "Protected archive · {name}",
            "ui.protected_archive_detail": "detail",
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
          },
        });
      }
      if (url.includes("/api/files")) {
        return jsonResponse({
          items: [
            {
              type: "file",
              name: "q1-summary.pdf",
              path: "reports/q1-summary.pdf",
              local_size: 1024,
              state: "both",
              storage_class: "STANDARD",
              cloud_exists: 1,
              local_exists: 1,
            },
          ],
          total: 1,
          page: 1,
          directory: "",
          mode: "browse",
        });
      }
      if (url.includes("/api/jobs")) {
        return jsonResponse({ groups: [] });
      }
      return jsonResponse({ detail: `unmocked ${url}` }, 404);
    });
  });

  afterEach(() => {
    cleanup();
    resetApiClientForTests();
  });

  function renderApp() {
    const client = createAppQueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <ApiQueryProvider client={client}>
        <I18nProvider>
          <App />
        </I18nProvider>
      </ApiQueryProvider>,
    );
  }

  it("renders the vault shell heading", async () => {
    renderApp();
    await waitFor(() => {
      expect(
        screen.getByRole("heading", { level: 1, name: "Test Archive" }),
      ).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByTestId("file-browser")).toBeInTheDocument();
    });
  });

  it("renders archive statistics and the file browser without burying the first file", async () => {
    renderApp();
    await waitFor(() => {
      expect(screen.getByTestId("stats-compact")).toBeInTheDocument();
    });
    expect(screen.getByTestId("archive-file-list")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByText("q1-summary.pdf").length).toBeGreaterThan(0);
    });
    expect(screen.getByTestId("safety-footer")).toBeInTheDocument();
  });
});
