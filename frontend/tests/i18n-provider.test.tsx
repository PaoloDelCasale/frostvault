import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { useI18n } from "@/i18n/useI18n";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);

function loadCatalog(locale: "en" | "it"): Record<string, string> {
  const raw = readFileSync(path.join(localesDir, `${locale}.json`), "utf8");
  return JSON.parse(raw) as Record<string, string>;
}

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function Probe() {
  const { t, setLocale, locale } = useI18n();
  return (
    <div>
      <p data-testid="label">{t("ui.sign_out")}</p>
      <p data-testid="locale">{locale}</p>
      <button type="button" onClick={() => void setLocale("it")}>
        switch
      </button>
    </div>
  );
}

function mockCatalogFetch(
  fetchMock: ReturnType<typeof vi.fn>,
  en: Record<string, string>,
  it: Record<string, string>,
): void {
  fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) {
      return Promise.resolve(
        jsonResponse({ locale: "en", locales: ["en", "it"], messages: en }),
      );
    }
    if (url === "/api/locale" && init?.method === "PUT") {
      return Promise.resolve(
        jsonResponse({
          locale: "it",
          message: it["api.locale_updated"],
          message_key: "api.locale_updated",
          messages: it,
        }),
      );
    }
    return Promise.reject(new Error(`unexpected request ${url}`));
  });
}

describe("I18nProvider locale switching", () => {
  const fetchMock = vi.fn();
  const reloadMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    reloadMock.mockReset();
    configureApiClient({ fetch: fetchMock });
    vi.stubGlobal("location", {
      ...window.location,
      reload: reloadMock,
    });
    document.documentElement.lang = "en";
  });

  afterEach(() => {
    cleanup();
    resetApiClientForTests();
    vi.unstubAllGlobals();
  });

  function renderProvider() {
    const client = createAppQueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <ApiQueryProvider client={client}>
        <I18nProvider>
          <Probe />
        </I18nProvider>
      </ApiQueryProvider>,
    );
  }

  it("calls PUT /api/locale and re-renders without a page reload", async () => {
    const en = loadCatalog("en");
    const it = loadCatalog("it");
    mockCatalogFetch(fetchMock, en, it);

    renderProvider();

    await waitFor(() => {
      expect(screen.getByTestId("label")).toHaveTextContent("Sign out");
    });

    fireEvent.click(screen.getByRole("button", { name: "switch" }));

    await waitFor(() => {
      expect(screen.getByTestId("label")).toHaveTextContent("Esci");
    });
    expect(screen.getByTestId("locale")).toHaveTextContent("it");

    const putCall = fetchMock.mock.calls.find(
      ([url, init]) => String(url) === "/api/locale" && init?.method === "PUT",
    );
    expect(putCall).toBeDefined();
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({ locale: "it" });
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it("keeps the html lang attribute in sync with the active locale", async () => {
    const en = loadCatalog("en");
    const it = loadCatalog("it");
    mockCatalogFetch(fetchMock, en, it);

    renderProvider();

    await waitFor(() => {
      expect(screen.getByTestId("label")).toHaveTextContent("Sign out");
    });
    expect(document.documentElement.lang).toBe("en");

    fireEvent.click(screen.getByRole("button", { name: "switch" }));

    await waitFor(() => {
      expect(document.documentElement.lang).toBe("it");
    });
  });
});
