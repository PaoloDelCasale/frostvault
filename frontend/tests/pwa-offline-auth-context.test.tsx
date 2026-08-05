import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
import {
  OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
  OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
  OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
  OFFLINE_FILE_CACHE_FINISH_TRANSITION_MESSAGE,
  OFFLINE_FILE_CACHE_GENERATION_MESSAGE,
  OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE,
  OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE,
  OFFLINE_FILE_CACHE_REPLY_TIMEOUT_MS,
  OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE,
} from "@/pwa/offlineFiles";

const OFFLINE_FILE_STORAGE_PREFIX = "frostvault.files.cache.v3:";

type WorkerGeneration = { bootId: string; counter: number };
type ServiceWorkerListener = (event: MessageEvent<unknown>) => void;
type ControllerChangeListener = () => void;

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
    offline_cache_generation: `session-${userId}-vault-${vaultId}-${csrfToken}`,
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

function installServiceWorkerProtocolHarness() {
  const messageListeners = new Set<ServiceWorkerListener>();
  const controllerChangeListeners = new Set<ControllerChangeListener>();
  const posted: Array<Record<string, unknown>> = [];
  let generation: WorkerGeneration = { bootId: "app-worker", counter: 1 };
  let workerClosed = false;
  let responsive = true;
  const activeTransitions = new Set<string>();

  function deliver(message: Record<string, unknown>) {
    const event = new MessageEvent("message", { data: message });
    for (const listener of messageListeners) listener(event);
  }

  const worker = {
    postMessage: vi.fn((message: Record<string, unknown>) => {
      posted.push(message);
      if (!responsive) return;
      const requestId = message.requestId;
      if (typeof requestId !== "string") return;
      if (message.type === OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE) {
        deliver({
          type: OFFLINE_FILE_CACHE_GENERATION_MESSAGE,
          requestId,
          generation,
          closed: workerClosed,
        });
        return;
      }
      if (message.type === OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE) {
        const transitionId = message.transitionId;
        if (typeof transitionId === "string" && !activeTransitions.has(transitionId)) {
          activeTransitions.add(transitionId);
          workerClosed = true;
          generation = { ...generation, counter: generation.counter + 1 };
        }
        deliver({
          type: OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE,
          requestId,
          generation,
          accepted: true,
          closed: true,
          transitionComplete: false,
        });
        return;
      }
      if (message.type === OFFLINE_FILE_CACHE_CONTEXT_MESSAGE) {
        const transitionId = message.transitionId;
        let accepted = false;
        let transitionComplete = false;
        if (!workerClosed && activeTransitions.size === 0 && !transitionId) {
          accepted = true;
        } else if (
          typeof transitionId === "string" &&
          activeTransitions.delete(transitionId) &&
          activeTransitions.size === 0
        ) {
          workerClosed = false;
          generation = { ...generation, counter: generation.counter + 1 };
          accepted = true;
          transitionComplete = true;
        }
        deliver({
          type: OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
          requestId,
          generation,
          accepted,
          closed: workerClosed,
          transitionComplete,
        });
        return;
      }
      if (message.type === OFFLINE_FILE_CACHE_FINISH_TRANSITION_MESSAGE) {
        const transitionId = message.transitionId;
        const accepted =
          typeof transitionId === "string" &&
          activeTransitions.delete(transitionId) &&
          activeTransitions.size === 0;
        if (accepted) {
          workerClosed = false;
          generation = { ...generation, counter: generation.counter + 1 };
        }
        deliver({
          type: OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE,
          requestId,
          generation,
          accepted,
          closed: workerClosed,
          transitionComplete: accepted,
        });
      }
    }),
  };

  vi.stubGlobal("navigator", {
    ...navigator,
    serviceWorker: {
      controller: worker,
      addEventListener: (type: string, listener: ServiceWorkerListener) => {
        if (type === "message") messageListeners.add(listener);
        if (type === "controllerchange") {
          controllerChangeListeners.add(listener as unknown as ControllerChangeListener);
        }
      },
      removeEventListener: (type: string, listener: ServiceWorkerListener) => {
        if (type === "message") messageListeners.delete(listener);
        if (type === "controllerchange") {
          controllerChangeListeners.delete(listener as unknown as ControllerChangeListener);
        }
      },
      getRegistration: vi.fn(async () => undefined),
    },
  });

  return {
    posted,
    invalidate(closed: boolean) {
      workerClosed = closed;
      generation = { ...generation, counter: generation.counter + 1 };
      deliver({
        type: OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE,
        generation,
        closed,
      });
    },
    controllerChange() {
      for (const listener of controllerChangeListeners) listener();
    },
    restartWithoutNotification() {
      activeTransitions.clear();
      workerClosed = true;
      generation = { bootId: "app-worker-restarted", counter: 1 };
    },
    setResponsive(value: boolean) {
      responsive = value;
    },
  };
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

function installDefaultApi(
  session: () => "a" | "b",
  timeline?: string[],
) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = requestUrl(input);
    if (url.startsWith("/api/i18n/catalog")) return jsonResponse(catalog);
    if (url === "/api/me") {
      timeline?.push("me");
      return jsonResponse(
        session() === "a"
          ? meResponse(11, 101, "Vault A")
          : meResponse(11, 202, "Vault B"),
      );
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
      timeline?.push("select");
      return jsonResponse({ vault_id: 202 });
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
      return jsonResponse(listing(session() === "a" ? "vault-a.txt" : "vault-b.txt"));
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
  configureApiClient({ fetch: fetchMock });
  return fetchMock;
}

describe("App offline cache authorization transitions", () => {
  beforeEach(() => {
    resetApiClientForTests();
    localStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    resetApiClientForTests();
    vi.unstubAllGlobals();
  });

  it("keeps offline listing availability across an unchanged context refresh", async () => {
    const worker = installServiceWorkerProtocolHarness();
    let current: "a" | "b" = "a";
    installDefaultApi(() => current);
    const user = userEvent.setup();

    renderApp();
    await waitFor(() => {
      expect(screen.getAllByText("vault-a.txt").length).toBeGreaterThan(0);
      expect(offlineFileStorageKeys()).not.toHaveLength(0);
    });
    const beginsBeforeLocale = worker.posted.filter(
      (message) => message.type === OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
    ).length;

    await user.selectOptions(
      screen.getAllByRole("combobox", { name: "Language" })[0]!,
      "it",
    );
    await waitFor(() => {
      expect(offlineFileStorageKeys()).not.toHaveLength(0);
      expect(screen.getAllByText("vault-a.txt").length).toBeGreaterThan(0);
    });
    expect(
      worker.posted.filter(
        (message) => message.type === OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
      ),
    ).toHaveLength(beginsBeforeLocale);
    current = "b";
  });

  it("begins a global close before Vault mutation and registers only the post-selection context", async () => {
    const worker = installServiceWorkerProtocolHarness();
    let current: "a" | "b" = "a";
    const timeline: string[] = [];
    let transitionStart = 0;
    const fetchMock = installDefaultApi(() => current, timeline);
    const originalSelect = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      if (url === "/api/vaults/select" && init?.method === "POST") {
        const beganBeforeMutation = worker.posted
          .slice(transitionStart)
          .some(
            (message) =>
              message.type === OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
          );
        timeline.push(beganBeforeMutation ? "begin-then-select" : "select-too-early");
        current = "b";
        return jsonResponse({ vault_id: 202 });
      }
      return originalSelect!(input, init);
    });
    const user = userEvent.setup();

    renderApp();
    await screen.findAllByText("vault-a.txt");
    const baseline = worker.posted.length;
    transitionStart = baseline;
    await user.selectOptions(
      screen.getAllByRole("combobox", { name: "Vault" })[0]!,
      "202",
    );

    await waitFor(() => {
      expect(timeline).toContain("begin-then-select");
      expect(screen.getAllByText("vault-b.txt").length).toBeGreaterThan(0);
    });
    const transitionMessages = worker.posted.slice(baseline);
    const beginIndex = transitionMessages.findIndex(
      (message) => message.type === OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
    );
    const completion = transitionMessages.find(
      (message) =>
        message.type === OFFLINE_FILE_CACHE_CONTEXT_MESSAGE &&
        typeof message.transitionId === "string",
    );
    expect(beginIndex).toBeGreaterThanOrEqual(0);
    expect(timeline).not.toContain("select-too-early");
    expect(completion).toMatchObject({
      context: { userId: 11, vaultId: 202 },
      transitionId: expect.any(String),
    });
    expect(offlineFileStorageKeys()).not.toHaveLength(0);
  });

  it("still sends the Vault mutation after a Worker ACK timeout while purging local data", async () => {
    const worker = installServiceWorkerProtocolHarness();
    let current: "a" | "b" = "a";
    const fetchMock = installDefaultApi(() => current);
    const original = fetchMock.getMockImplementation();
    let selected = false;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      if (url === "/api/vaults/select" && init?.method === "POST") {
        selected = true;
        current = "b";
        return jsonResponse({ vault_id: 202 });
      }
      return original!(input, init);
    });

    renderApp();
    await screen.findAllByText("vault-a.txt");
    expect(offlineFileStorageKeys()).not.toHaveLength(0);
    vi.useFakeTimers();
    worker.setResponsive(false);

    fireEvent.change(screen.getAllByRole("combobox", { name: "Vault" })[0]!, {
      target: { value: "202" },
    });
    expect(offlineFileStorageKeys()).toEqual([]);
    expect(selected).toBe(false);

    await vi.advanceTimersByTimeAsync(OFFLINE_FILE_CACHE_REPLY_TIMEOUT_MS);
    await Promise.resolve();
    expect(selected).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          requestUrl(input as RequestInfo | URL) === "/api/vaults/select" &&
          (init as RequestInit | undefined)?.method === "POST",
      ),
    ).toBe(true);
  });

  it("keeps the authenticated shell landmark mounted while another client closes the generation", async () => {
    const worker = installServiceWorkerProtocolHarness();
    const current: "a" | "b" = "a";
    installDefaultApi(() => current);
    const app = renderApp();
    await screen.findAllByText("vault-a.txt");
    expect(offlineFileStorageKeys()).not.toHaveLength(0);

    worker.invalidate(true);
    await waitFor(() => {
      expect(
        screen.getByRole("link", { name: /skip to main content/i }),
      ).toBeInTheDocument();
      expect(document.getElementById("main-content")).toBeInTheDocument();
      expect(screen.queryByTestId("file-browser")).not.toBeInTheDocument();
      expect(offlineFileStorageKeys()).toEqual([]);
      expect(
        app.client.getQueryCache().findAll({ queryKey: ["files"] }),
      ).toEqual([]);
    });
  });

  it("restores file-list readiness after a completed Worker reconciliation", async () => {
    const worker = installServiceWorkerProtocolHarness();
    const current: "a" | "b" = "a";
    installDefaultApi(() => current);
    renderApp();
    await screen.findAllByText("vault-a.txt");

    worker.invalidate(false);
    await waitFor(() => {
      expect(screen.getByTestId("file-browser")).toBeInTheDocument();
      expect(screen.getAllByText("vault-a.txt").length).toBeGreaterThan(0);
      expect(
        screen.getByRole("link", { name: /skip to main content/i }),
      ).toBeInTheDocument();
      expect(document.getElementById("main-content")).toBeInTheDocument();
    });
  });

  it("retries a reconciliation when Worker control changes before authority settles", async () => {
    const worker = installServiceWorkerProtocolHarness();
    const current: "a" | "b" = "a";
    let vaultsRequested = false;
    let releaseVaults!: () => void;
    const vaultsReady = new Promise<void>((resolve) => {
      releaseVaults = resolve;
    });
    const fetchMock = installDefaultApi(() => current);
    const original = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (
        requestUrl(input) === "/api/vaults" &&
        (!init?.method || init.method === "GET")
      ) {
        vaultsRequested = true;
        await vaultsReady;
      }
      return original!(input, init);
    });

    renderApp();
    await waitFor(() => expect(vaultsRequested).toBe(true));
    worker.controllerChange();
    releaseVaults();

    await waitFor(() => {
      expect(screen.getByTestId("file-browser")).toBeInTheDocument();
      expect(screen.getAllByText("vault-a.txt").length).toBeGreaterThan(0);
      expect(
        screen.getByRole("link", { name: /skip to main content/i }),
      ).toBeInTheDocument();
      expect(document.getElementById("main-content")).toBeInTheDocument();
    });
  });

  it("discovers an ordinary Worker restart on focus before reusing a local payload", async () => {
    const worker = installServiceWorkerProtocolHarness();
    const current: "a" | "b" = "a";
    const fetchMock = installDefaultApi(() => current);
    const original = fetchMock.getMockImplementation();
    let filesOffline = false;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      if (filesOffline && url.startsWith("/api/files")) {
        throw new TypeError("offline after Worker restart");
      }
      return original!(input, init);
    });

    renderApp();
    await screen.findAllByText("vault-a.txt");
    expect(offlineFileStorageKeys()).not.toHaveLength(0);

    // No Worker invalidation/controllerchange is delivered. The page's bounded
    // focus handshake sees only the new boot nonce and closed initial barrier.
    filesOffline = true;
    worker.restartWithoutNotification();
    window.dispatchEvent(new Event("focus"));

    await waitFor(() => {
      expect(offlineFileStorageKeys()).toEqual([]);
      expect(screen.queryByText("vault-a.txt")).not.toBeInTheDocument();
      expect(screen.getByTestId("offline-shell")).toHaveTextContent(
        "You are offline.",
      );
    });
    expect(
      worker.posted.some(
        (message) =>
          message.type === OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
      ),
    ).toBe(true);
  });
});
