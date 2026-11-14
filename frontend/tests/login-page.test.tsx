import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  configureApiClient,
  resetApiClientForTests,
} from "@/api";
import { I18nContext, type I18nContextValue } from "@/i18n/context";
import { translate } from "@/i18n/translate";
import { LoginPage } from "@/pages/login/LoginPage";
import { ThemeProvider } from "@/theme";
import {
  THEME_ACTIVE_USER_STORAGE_KEY,
  themeStorageKey,
} from "@/theme/theme";

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

describe("LoginPage local sign-in", () => {
  const fetchMock = vi.fn();
  const navigate = vi.fn();
  const en = loadCatalog("en");
  let options: unknown;
  let loginResponse: Response;

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    navigate.mockReset();
    window.localStorage.clear();
    options = { oidc_enabled: true, oidc_provider_name: null };
    loginResponse = jsonResponse({ message_key: "api.signed_in", message: "Signed in" });
    fetchMock.mockImplementation(async (url: string) =>
      url === "/api/login/options" ? (options instanceof Response ? options : jsonResponse(options)) : loginResponse);
    configureApiClient({ fetch: fetchMock });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    window.localStorage.clear();
    resetApiClientForTests();
  });

  function renderPage(i18nOverrides: Partial<I18nContextValue> = {}) {
    const value: I18nContextValue = {
      locale: "en",
      locales: ["en", "it"],
      ready: true,
      t: (key, params) => translate(en, key, params),
      setLocale: vi.fn(async () => undefined),
      ...i18nOverrides,
    };
    return render(
      <I18nContext.Provider value={value}>
        <ThemeProvider>
          <LoginPage onNavigate={navigate} />
        </ThemeProvider>
      </I18nContext.Provider>,
    );
  }

  function loginCalls() {
    return fetchMock.mock.calls.filter(([url]) => url === "/api/login");
  }

  it("makes OIDC the primary sign-in and keeps the Emergency Password secondary", async () => {
    renderPage();

    const oidc = await screen.findByRole("button", { name: en["login.oidc"] });
    expect(oidc).toHaveAttribute("data-variant", "primary");
    const emergency = screen.getByRole("region", { name: en["login.emergency_title"] });
    expect(emergency).toHaveTextContent(en["login.emergency_help"]);
    expect(screen.getByRole("button", { name: en["login.emergency_submit"] })).toHaveAttribute("data-variant", "secondary");
    expect(screen.queryByText(/Pocket ID|loopback|Break-glass/i)).not.toBeInTheDocument();
  });

  it("names the configured identity provider on the OIDC button", async () => {
    options = { oidc_enabled: true, oidc_provider_name: "Pocket ID" };
    renderPage();

    expect(await screen.findByRole("button", { name: "Sign in with Pocket ID" })).toHaveAttribute("data-variant", "primary");
  });

  it("keeps the password form primary and hides OIDC when it is not configured", async () => {
    options = { oidc_enabled: false, oidc_provider_name: null };
    renderPage();

    expect(await screen.findByRole("button", { name: en["login.submit"] })).toHaveAttribute("data-variant", "primary");
    expect(screen.getByText(en["login.password_help"])).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: en["login.oidc"] })).not.toBeInTheDocument();
  });

  it("offers both methods with the password first when the options cannot be read", async () => {
    options = jsonResponse({ detail: "boom" }, 500);
    renderPage();

    expect(await screen.findByRole("button", { name: en["login.submit"] })).toHaveAttribute("data-variant", "primary");
    expect(screen.getByRole("button", { name: en["login.oidc"] })).toHaveAttribute("data-variant", "secondary");
  });

  it("submits the Emergency Password to POST /api/login and redirects to the archive on success", async () => {
    renderPage();

    fireEvent.change(await screen.findByLabelText(en["login.password"]), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.click(screen.getByRole("button", { name: en["login.emergency_submit"] }));

    await waitFor(() => {
      expect(loginCalls()).toHaveLength(1);
    });
    const [, init] = loginCalls()[0]!;
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({
      password: "correct-horse-battery",
    });
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("/");
    });
  });

  it("clears the active theme on the login screen without removing Owner preferences", async () => {
    localStorage.setItem(THEME_ACTIVE_USER_STORAGE_KEY, "42");
    localStorage.setItem(themeStorageKey(42), "dark");
    renderPage();
    await screen.findByRole("button", { name: en["login.oidc"] });
    expect(localStorage.getItem(THEME_ACTIVE_USER_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(themeStorageKey(42))).toBe("dark");
    expect(loginCalls()).toHaveLength(0);
  });

  it("navigates directly to OIDC without a Worker handshake", async () => {
    renderPage();
    navigate.mockImplementation(() => {
      expect(window.localStorage.getItem(THEME_ACTIVE_USER_STORAGE_KEY)).toBeNull();
    });

    fireEvent.click(await screen.findByRole("button", { name: en["login.oidc"] }));

    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("/auth/oidc/login");
    });
  });

  it("shows a localized error and does not redirect when credentials are wrong", async () => {
    loginResponse = jsonResponse({ detail: "Incorrect password" }, 401);
    renderPage();

    fireEvent.change(await screen.findByLabelText(en["login.password"]), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: en["login.emergency_submit"] }));

    expect(await screen.findByRole("alert")).toHaveTextContent(en["login.failed"]);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("explains Emergency Password network gating when the backend refuses with 403", async () => {
    loginResponse = jsonResponse(
      { detail: "Break-glass login is not allowed from this network" },
      403,
    );
    renderPage();

    fireEvent.change(await screen.findByLabelText(en["login.password"]), {
      target: { value: "secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: en["login.emergency_submit"] }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      en["login.local_unavailable"],
    );
    expect(navigate).not.toHaveBeenCalled();
  });

  it("changes language via the locale switcher without losing typed input", async () => {
    const it = loadCatalog("it");

    function LocaleHarness() {
      const [locale, setLocaleState] = useState<"en" | "it">("en");
      const messages = locale === "it" ? it : en;
      const value: I18nContextValue = {
        locale,
        locales: ["en", "it"],
        ready: true,
        t: (key, params) => translate(messages, key, params),
        setLocale: async (next) => {
          setLocaleState(next as "en" | "it");
        },
      };
      return (
        <I18nContext.Provider value={value}>
          <LoginPage onNavigate={navigate} />
        </I18nContext.Provider>
      );
    }

    render(<LocaleHarness />);

    fireEvent.change(await screen.findByLabelText(en["login.password"]), {
      target: { value: "typed-pass" },
    });

    fireEvent.change(screen.getByLabelText(en["ui.language"]), {
      target: { value: "it" },
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: it["login.emergency_submit"] })).toBeInTheDocument();
    });
    expect(screen.getByText(it["login.emergency_help"])).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: it["login.oidc"] }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/username|nome utente/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(it["login.password"])).toHaveValue("typed-pass");
  });
});
