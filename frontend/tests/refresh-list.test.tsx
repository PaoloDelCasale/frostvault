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

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const stats = {
  states: { both: 0, local_only: 0, cloud_only: 0 },
  storage: { local_bytes: 0, cloud_bytes: 0 },
  active_jobs: 0,
  runtime: {},
  filesystem: null,
  delete_enabled: false,
};

const catalog = {
  locale: "en",
  locales: ["en", "it"],
  messages: {
    "ui.refresh_list": "Refresh list",
    "ui.refresh_list_failed": "Could not refresh the list.",
  },
};

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.href
      : input.url;
}

describe("Refresh list", () => {
  const fetchMock = vi.fn();
  let scanResponse: Response;
  let filesCalls: number;
  let statsCalls: number;

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    scanResponse = jsonResponse(
      { message: "Scan started", message_key: "api.scan_started" },
      202,
    );
    filesCalls = 0;
    statsCalls = 0;
    configureApiClient({ fetch: fetchMock });
    window.history.replaceState({}, "", "/");
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.includes("/api/me")) {
        return jsonResponse({
          id: 1,
          username: "operator",
          display_name: "Operator",
          is_admin: false,
          active: true,
          session_version: 1,
          csrf_token: "csrf",
          offline_cache_generation: "operator-session-vault-1",
          auth_method: "local",
          locale: "en",
          locales: ["en", "it"],
          vault: {
            id: 1,
            slug: "test",
            name: "Test Archive",
            role: "operator",
            can_operate: true,
            delete_enabled: false,
            cloud_deletion_enabled: false,
            is_vault_owner: false,
          },
        });
      }
      if (url.includes("/api/vaults")) {
        return jsonResponse({
          items: [{ id: 1, slug: "test", name: "Test Archive", role: "operator" }],
        });
      }
      if (url.includes("/api/i18n/catalog")) return jsonResponse(catalog);
      if (url.includes("/api/scan")) return scanResponse;
      if (url.includes("/api/files")) {
        filesCalls += 1;
        return jsonResponse({
          items: [],
          total: 0,
          page: 1,
          directory: "",
          mode: "browse",
        });
      }
      if (url.includes("/api/stats")) {
        statsCalls += 1;
        return jsonResponse(stats);
      }
      if (url.includes("/api/jobs")) return jsonResponse({ groups: [] });
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

  it("POSTs the scan and refetches files and stats after the accepted 202", async () => {
    const user = userEvent.setup();
    renderApp();

    const refresh = await screen.findByRole("button", { name: "Refresh list" });
    await waitFor(() => {
      expect(filesCalls).toBeGreaterThan(0);
      expect(statsCalls).toBeGreaterThan(0);
    });

    await user.click(refresh);

    await waitFor(() => {
      const scanCall = fetchMock.mock.calls.find(
        ([input, init]) =>
          requestUrl(input as RequestInfo | URL).includes("/api/scan") &&
          (init as RequestInit | undefined)?.method === "POST",
      );
      expect(scanCall).toBeDefined();
      expect(filesCalls).toBeGreaterThan(1);
      expect(statsCalls).toBeGreaterThan(1);
    });
  });

  it("shows a localized failure alert without leaving a rejected click promise", async () => {
    const user = userEvent.setup();
    scanResponse = jsonResponse({ detail: "backend failure" }, 500);
    renderApp();

    await user.click(
      await screen.findByRole("button", { name: "Refresh list" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not refresh the list.",
    );
  });
});
