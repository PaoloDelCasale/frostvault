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

describe("LoginPage Break-glass Login submit", () => {
  const fetchMock = vi.fn();
  const navigate = vi.fn();
  const en = loadCatalog("en");

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    navigate.mockReset();
    configureApiClient({ fetch: fetchMock });
  });

  afterEach(() => {
    cleanup();
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
        <LoginPage onNavigate={navigate} />
      </I18nContext.Provider>,
    );
  }

  it("submits Break-glass credentials to POST /api/login and redirects to the archive on success", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ message_key: "api.signed_in", message: "Signed in" }),
    );
    renderPage();

    fireEvent.change(screen.getByLabelText(en["login.username"]), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText(en["login.password"]), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.click(screen.getByRole("button", { name: en["login.submit"] }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/login");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({
      username: "admin",
      password: "correct-horse-battery",
    });
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("/");
    });
  });

  it("shows a localized error and does not redirect when credentials are wrong", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "Incorrect username or password" }, 401),
    );
    renderPage();

    fireEvent.change(screen.getByLabelText(en["login.username"]), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText(en["login.password"]), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: en["login.submit"] }));

    expect(await screen.findByRole("alert")).toHaveTextContent(en["login.failed"]);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("explains Break-glass network gating when the backend refuses with 403", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { detail: "Break-glass login is not allowed from this network" },
        403,
      ),
    );
    renderPage();

    fireEvent.change(screen.getByLabelText(en["login.username"]), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText(en["login.password"]), {
      target: { value: "secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: en["login.submit"] }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      en["login.local_unavailable"],
    );
    expect(navigate).not.toHaveBeenCalled();
  });

  it("navigates to /auth/oidc/login when the OIDC button is used", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: en["login.oidc"] }));
    expect(navigate).toHaveBeenCalledWith("/auth/oidc/login");
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

    fireEvent.change(screen.getByLabelText(en["login.username"]), {
      target: { value: "typed-user" },
    });
    fireEvent.change(screen.getByLabelText(en["login.password"]), {
      target: { value: "typed-pass" },
    });

    fireEvent.change(screen.getByLabelText(en["ui.language"]), {
      target: { value: "it" },
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: it["login.submit"] })).toBeInTheDocument();
    });
    expect(screen.getByLabelText(it["login.username"])).toHaveValue("typed-user");
    expect(screen.getByLabelText(it["login.password"])).toHaveValue("typed-pass");
  });
});
