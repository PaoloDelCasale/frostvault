import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import App from "@/App";
import { I18nProvider } from "@/i18n/I18nProvider";
import { OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE } from "@/pwa/offlineFiles";

const OFFLINE_FILE_STORAGE_PREFIX = "frostvault.files.cache.v2:";

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.href
      : input.url;
}

function meResponse(
  userId: number,
  vaultId: number,
  vaultName: string,
  csrfToken = `csrf-${userId}`,
) {
  return {
    id: userId,
    username: `user-${userId}`,
    display_name: `User ${userId}`,
    is_admin: false,
    active: true,
    session_version: 1,
    csrf_token: csrfToken,
    auth_method: "local",
    locale: "en",
    locales: ["en", "it"],
    vault: {
      id: vaultId,
      slug: `vault-${vaultId}`,
      name: vaultName,
      role: "owner",
      can_operate: true,
      delete_enabled: false,
      cloud_deletion_enabled: false,
      is_vault_owner: true,
    },
  };
}

function listing(name: string) {
  return {
    items: [
      {
        type: "file",
        name,
        path: name,
        state: "both",
        local_size: 12,
      },
    ],
    total: 1,
    page: 1,
    directory: "",
    mode: "browse",
  };
}

const catalog = {
  locale: "en",
  locales: ["en", "it"],
  messages: {
    "ui.vault": "Vault",
    "ui.sign_out": "Sign out",
    "ui.loading": "Loading…",
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
  },
};

function offlineFileStorageKeys(): string[] {
  return Array.from({ length: localStorage.length }, (_, index) =>
    localStorage.key(index),
  ).filter((key): key is string => key?.startsWith(OFFLINE_FILE_STORAGE_PREFIX) ?? false);
}

function renderApp() {
  const client = createAppQueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    client,
    ...render(
      <ApiQueryProvider client={client}>
        <I18nProvider>
          <App />
        </I18nProvider>
      </ApiQueryProvider>,
    ),
  };
}

function installServiceWorkerMessageHarness() {
  const listeners = new Set<(event: MessageEvent<unknown>) => void>();
  vi.stubGlobal("navigator", {
    ...navigator,
    serviceWorker: {
      addEventListener: (type: string, listener: (event: MessageEvent<unknown>) => void) => {
        if (type === "message") listeners.add(listener);
      },
      removeEventListener: (
        type: string,
        listener: (event: MessageEvent<unknown>) => void,
      ) => {
        if (type === "message") listeners.delete(listener);
      },
      getRegistration: vi.fn(async () => undefined),
    },
  });
  return {
    invalidate(epoch: number) {
      const event = new MessageEvent("message", {
        data: { type: OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE, epoch },
      });
      for (const listener of listeners) listener(event);
    },
  };
}

describe("App offline cache authorization transitions", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    configureApiClient({ fetch: fetchMock });
    localStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  afterEach(() => {
    cleanup();
    resetApiClientForTests();
    vi.unstubAllGlobals();
  });

  it("clears User A's listing on logout before a new App authenticates User B", async () => {
    const user = userEvent.setup();
    let session: "a" | "b" = "a";
    const logoutPending = new Promise<Response>(() => undefined);
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      if (url.startsWith("/api/i18n/catalog")) return jsonResponse(catalog);
      if (url === "/api/me") {
        return jsonResponse(
          session === "a"
            ? meResponse(11, 101, "Vault A")
            : meResponse(22, 202, "Vault B"),
        );
      }
      if (url === "/api/vaults" && (!init?.method || init.method === "GET")) {
        return jsonResponse({
          items:
            session === "a"
              ? [{ id: 101, slug: "vault-101", name: "Vault A", role: "owner" }]
              : [{ id: 202, slug: "vault-202", name: "Vault B", role: "owner" }],
        });
      }
      if (url.startsWith("/api/files")) {
        if (session === "b") throw new TypeError("offline");
        return jsonResponse(listing("user-a.txt"));
      }
      if (url === "/api/jobs") return jsonResponse({ items: [], groups: [] });
      if (url === "/api/stats") {
        return jsonResponse({
          states: { both: 0, local_only: 0, cloud_only: 0 },
          storage: { local_bytes: 0, cloud_bytes: 0 },
          active_jobs: 0,
          runtime: {},
          filesystem: null,
          delete_enabled: false,
        });
      }
      if (url === "/api/logout" && init?.method === "POST") return logoutPending;
      return jsonResponse({ detail: `unexpected ${url}` }, 404);
    });

    const firstApp = renderApp();
    await waitFor(() => {
      expect(screen.getAllByText("user-a.txt").length).toBeGreaterThan(0);
      expect(offlineFileStorageKeys()).not.toHaveLength(0);
    });

    await user.click(screen.getAllByRole("button", { name: "Sign out" })[0]!);
    await waitFor(() => {
      expect(offlineFileStorageKeys()).toEqual([]);
      expect(screen.queryByTestId("file-browser")).not.toBeInTheDocument();
    });

    firstApp.unmount();
    session = "b";
    vi.stubGlobal("navigator", { ...navigator, onLine: false });
    renderApp();

    expect(await screen.findByTestId("offline-shell")).toHaveTextContent(
      "You are offline.",
    );
    expect(screen.queryByText("user-a.txt")).not.toBeInTheDocument();
  });

  it("stays behind the barrier when Vault selection succeeds but fresh session loading fails", async () => {
    const user = userEvent.setup();
    let selectionSucceeded = false;
    let meCalls = 0;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      if (url.startsWith("/api/i18n/catalog")) return jsonResponse(catalog);
      if (url === "/api/me") {
        meCalls += 1;
        return selectionSucceeded
          ? jsonResponse({ detail: "session refresh failed" }, 500)
          : jsonResponse(meResponse(11, 101, "Vault A"));
      }
      if (url === "/api/vaults" && (!init?.method || init.method === "GET")) {
        return jsonResponse({
          items: [
            { id: 101, slug: "vault-101", name: "Vault A", role: "owner" },
            { id: 202, slug: "vault-202", name: "Vault B", role: "owner" },
          ],
        });
      }
      if (url === "/api/vaults/select" && init?.method === "POST") {
        selectionSucceeded = true;
        return jsonResponse({ vault_id: 202 });
      }
      if (url.startsWith("/api/files")) return jsonResponse(listing("vault-a.txt"));
      if (url === "/api/jobs") return jsonResponse({ items: [], groups: [] });
      if (url === "/api/stats") {
        return jsonResponse({
          states: { both: 0, local_only: 0, cloud_only: 0 },
          storage: { local_bytes: 0, cloud_bytes: 0 },
          active_jobs: 0,
          runtime: {},
          filesystem: null,
          delete_enabled: false,
        });
      }
      return jsonResponse({ detail: `unexpected ${url}` }, 404);
    });

    renderApp();
    await waitFor(() => {
      expect(screen.getAllByText("vault-a.txt").length).toBeGreaterThan(0);
      expect(offlineFileStorageKeys()).not.toHaveLength(0);
    });

    await waitFor(() => {
      expect(screen.getAllByRole("option", { name: "Vault B" }).length).toBeGreaterThan(0);
    });
    const vaultPicker = screen.getAllByRole("combobox", { name: "Vault" })[0]!;
    await user.selectOptions(vaultPicker, "202");

    await waitFor(() => {
      expect(selectionSucceeded).toBe(true);
      expect(meCalls).toBeGreaterThan(1);
      expect(offlineFileStorageKeys()).toEqual([]);
      expect(screen.queryByTestId("file-browser")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("vault-a.txt")).not.toBeInTheDocument();
  });

  it("delivers one Worker invalidation payload to two mounted Apps and clears both file query clients", async () => {
    const worker = installServiceWorkerMessageHarness();
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      if (url.startsWith("/api/i18n/catalog")) return jsonResponse(catalog);
      if (url === "/api/me") return jsonResponse(meResponse(11, 101, "Vault A"));
      if (url === "/api/vaults" && (!init?.method || init.method === "GET")) {
        return jsonResponse({
          items: [{ id: 101, slug: "vault-101", name: "Vault A", role: "owner" }],
        });
      }
      if (url.startsWith("/api/files")) return jsonResponse(listing("shared-tab.txt"));
      if (url === "/api/jobs") return jsonResponse({ items: [], groups: [] });
      if (url === "/api/stats") {
        return jsonResponse({
          states: { both: 0, local_only: 0, cloud_only: 0 },
          storage: { local_bytes: 0, cloud_bytes: 0 },
          active_jobs: 0,
          runtime: {},
          filesystem: null,
          delete_enabled: false,
        });
      }
      return jsonResponse({ detail: `unexpected ${url}` }, 404);
    });

    const firstApp = renderApp();
    const secondApp = renderApp();
    await waitFor(() => {
      expect(screen.getAllByText("shared-tab.txt")).toHaveLength(2);
      expect(offlineFileStorageKeys()).not.toHaveLength(0);
    });

    worker.invalidate(7);

    await waitFor(() => {
      expect(screen.queryAllByTestId("file-browser")).toHaveLength(0);
      expect(offlineFileStorageKeys()).toEqual([]);
      expect(firstApp.client.getQueryCache().findAll({ queryKey: ["files"] })).toEqual([]);
      expect(secondApp.client.getQueryCache().findAll({ queryKey: ["files"] })).toEqual([]);
    });
    expect(screen.getAllByText("Loading…")).toHaveLength(2);
  });

  it("preserves listings on unchanged locale/navigation refreshes but purges before a same-scope Session replacement", async () => {
    const user = userEvent.setup();
    let csrfToken = "session-a";
    let meCalls = 0;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      if (url.startsWith("/api/i18n/catalog")) return jsonResponse(catalog);
      if (url === "/api/me") {
        meCalls += 1;
        return jsonResponse(meResponse(11, 101, "Vault A", csrfToken));
      }
      if (url === "/api/vaults" && (!init?.method || init.method === "GET")) {
        return jsonResponse({
          items: [{ id: 101, slug: "vault-101", name: "Vault A", role: "owner" }],
        });
      }
      if (url === "/api/locale" && init?.method === "PUT") {
        return jsonResponse({
          locale: "it",
          message: "Locale updated",
          message_key: "api.locale_updated",
          messages: catalog.messages,
        });
      }
      if (url.startsWith("/api/notifications")) {
        return jsonResponse({ items: [], unread_count: 0 });
      }
      if (url.startsWith("/api/files")) {
        if (csrfToken === "session-b") throw new TypeError("offline after replacement");
        return jsonResponse(listing("before-replacement.txt"));
      }
      if (url === "/api/jobs") return jsonResponse({ items: [], groups: [] });
      if (url === "/api/stats") {
        return jsonResponse({
          states: { both: 0, local_only: 0, cloud_only: 0 },
          storage: { local_bytes: 0, cloud_bytes: 0 },
          active_jobs: 0,
          runtime: {},
          filesystem: null,
          delete_enabled: false,
        });
      }
      return jsonResponse({ detail: `unexpected ${url}` }, 404);
    });

    renderApp();
    await waitFor(() => {
      expect(screen.getAllByText("before-replacement.txt").length).toBeGreaterThan(0);
      expect(offlineFileStorageKeys()).not.toHaveLength(0);
    });

    const languagePicker = screen.getAllByRole("combobox", {
      name: "Language",
    })[0]!;
    await user.selectOptions(languagePicker, "it");
    await waitFor(() => {
      expect(meCalls).toBeGreaterThanOrEqual(2);
      expect(offlineFileStorageKeys()).not.toHaveLength(0);
    });

    window.history.pushState({}, "", "/vaults/new");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await waitFor(() => {
      expect(meCalls).toBeGreaterThanOrEqual(3);
      expect(offlineFileStorageKeys()).not.toHaveLength(0);
    });

    csrfToken = "session-b";
    window.history.pushState({}, "", "/");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await waitFor(() => {
      // The first changed-token response clears; the retry is a fresh /api/me
      // under the post-clear epoch before the archive can mount again.
      expect(meCalls).toBeGreaterThanOrEqual(5);
      expect(offlineFileStorageKeys()).toEqual([]);
    });
    expect(screen.queryByText("before-replacement.txt")).not.toBeInTheDocument();
  });
});
