import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import type { FilesResponse } from "@/api/types";
import { FileBrowser } from "@/pages/archive/FileBrowser";
import { saveCachedFilesListing } from "@/pwa/offlineFiles";

const messages: Record<string, string> = {
  "ui.search_placeholder": "Search",
  "ui.filter_by_state": "Filter",
  "ui.all_items": "All",
  "ui.go_up": "Up",
  "ui.up": "↑ Up",
  "ui.name": "Name",
  "ui.size": "Size",
  "ui.state": "State",
  "ui.cloud_storage": "Cloud",
  "ui.previous": "Previous",
  "ui.next": "Next",
  "ui.page_label": "Page {page} of {pages} · {total} {unit}",
  "ui.items_unit": "items",
  "ui.files_found_unit": "files found",
  "ui.empty_no_files": "Empty",
  "ui.empty_no_matches": "No matches",
  "ui.breadcrumb_archive": "Archive",
  "ui.file_list_placeholder": "Loading",
  "ui.offline_shell": "You are offline.",
  "ui.offline_stale_listing": "Showing stale listing.",
  "ui.more_actions": "More",
  "ui.cancel": "Cancel",
  "state.both": "Both",
  "state.filter.local_only": "Local",
  "state.filter.both": "Both",
  "state.filter.cloud_only": "Cloud",
  "state.filter.restoring": "Restoring",
};

function t(key: string): string {
  return messages[key] ?? key;
}

const offlineContext = { userId: 7, vaultId: 1 };

const listing: FilesResponse = {
  mode: "browse",
  directory: "",
  page: 1,
  total: 1,
  items: [
    {
      type: "file",
      name: "readme.txt",
      path: "readme.txt",
      state: "both",
      local_size: 10,
    },
  ],
};

describe("FileBrowser offline shell and stale listing (seams 2–3)", () => {
  afterEach(() => {
    cleanup();
    resetApiClientForTests();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  beforeEach(() => {
    resetApiClientForTests();
    localStorage.clear();
  });

  it("shows the offline shell when only another User's listing is cached", async () => {
    vi.stubGlobal("navigator", { ...navigator, onLine: false });
    saveCachedFilesListing(
      { userId: offlineContext.userId + 1, vaultId: offlineContext.vaultId },
      { directory: "", page: 1, page_size: 100 },
      listing,
    );
    configureApiClient({
      fetchImpl: async () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const client = createAppQueryClient();
    render(
      <ApiQueryProvider client={client}>
        <FileBrowser
          t={t}
          userId={offlineContext.userId}
          vaultId={offlineContext.vaultId}
          vaultName="Docs"
          capabilities={{
            can_operate: true,
            delete_enabled: false,
            cloud_deletion_enabled: false,
            is_vault_owner: true,
            role: "owner",
          }}
        />
      </ApiQueryProvider>,
    );
    await waitFor(() => {
      expect(screen.getByTestId("offline-shell")).toHaveTextContent(
        "You are offline.",
      );
    });
  });

  it("shows a stale listing banner when a cached listing is reused offline", async () => {
    vi.stubGlobal("navigator", { ...navigator, onLine: false });
    saveCachedFilesListing(
      offlineContext,
      { directory: "", page: 1, page_size: 100 },
      listing,
    );
    configureApiClient({
      fetchImpl: async () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const client = createAppQueryClient();
    render(
      <ApiQueryProvider client={client}>
        <FileBrowser
          t={t}
          userId={offlineContext.userId}
          vaultId={offlineContext.vaultId}
          vaultName="Docs"
          capabilities={{
            can_operate: true,
            delete_enabled: false,
            cloud_deletion_enabled: false,
            is_vault_owner: true,
            role: "owner",
          }}
        />
      </ApiQueryProvider>,
    );
    await waitFor(() => {
      expect(screen.getByTestId("offline-stale-banner")).toHaveTextContent(
        "Showing stale listing.",
      );
    });
    expect(screen.getAllByText("readme.txt").length).toBeGreaterThan(0);
  });
});
