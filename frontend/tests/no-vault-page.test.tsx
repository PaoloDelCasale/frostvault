import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  configureApiClient,
  resetApiClientForTests,
} from "@/api";
import { I18nContext, type I18nContextValue } from "@/i18n/context";
import { translate } from "@/i18n/translate";
import { NoVaultPage } from "@/pages/no-vault/NoVaultPage";
import { ThemeProvider } from "@/theme";
import { THEME_ACTIVE_USER_STORAGE_KEY } from "@/theme/theme";

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

describe("NoVaultPage", () => {
  const fetchMock = vi.fn();
  const navigate = vi.fn();
  const en = loadCatalog("en");
  const itMessages = loadCatalog("it");

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    navigate.mockReset();
    window.localStorage.clear();
    document.cookie = "frostvault_csrf=test-csrf";
    configureApiClient({ fetch: fetchMock });
  });

  afterEach(() => {
    cleanup();
    window.localStorage.clear();
    document.cookie = "frostvault_csrf=";
    resetApiClientForTests();
  });

  function renderPage(locale: "en" | "it" = "en") {
    const messages = locale === "it" ? itMessages : en;
    const value: I18nContextValue = {
      locale,
      locales: ["en", "it"],
      ready: true,
      t: (key, params) => translate(messages, key, params),
      setLocale: vi.fn(async () => undefined),
    };
    return render(
      <I18nContext.Provider value={value}>
        <ThemeProvider>
          <NoVaultPage onNavigate={navigate} />
        </ThemeProvider>
      </I18nContext.Provider>,
    );
  }

  it("shows localized create-vault and sign-out actions in English and Italian", () => {
    const { unmount } = renderPage("en");
    expect(screen.getByRole("heading", { name: en["no_vault.title"] })).toBeInTheDocument();
    expect(screen.getByText(en["no_vault.subtitle"])).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: en["no_vault.create"] }),
    ).toHaveAttribute("href", "/vaults/new");
    expect(
      screen.getByRole("button", { name: en["ui.sign_out"] }),
    ).toBeInTheDocument();
    unmount();

    renderPage("it");
    expect(screen.getByRole("heading", { name: itMessages["no_vault.title"] })).toBeInTheDocument();
    expect(screen.getByText(itMessages["no_vault.subtitle"])).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: itMessages["no_vault.create"] }),
    ).toHaveAttribute("href", "/vaults/new");
    expect(
      screen.getByRole("button", { name: itMessages["ui.sign_out"] }),
    ).toBeInTheDocument();
  });

  it("closes local file data before POST /api/logout then navigates to sign-in", async () => {
    const staleCacheKey = "frostvault.files.cache.v3:prior-session";
    localStorage.setItem(staleCacheKey, "prior-user-listing");
    fetchMock.mockImplementationOnce(() => {
      expect(localStorage.getItem(staleCacheKey)).toBeNull();
      return Promise.resolve(
        jsonResponse({ message_key: "api.signed_out", message: "Signed out" }),
      );
    });
    renderPage("en");
    window.localStorage.setItem(THEME_ACTIVE_USER_STORAGE_KEY, "42");
    navigate.mockImplementation(() => {
      expect(window.localStorage.getItem(THEME_ACTIVE_USER_STORAGE_KEY)).toBeNull();
    });

    fireEvent.click(screen.getByRole("button", { name: en["ui.sign_out"] }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/logout");
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("test-csrf");
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("/login");
    });
  });
});
