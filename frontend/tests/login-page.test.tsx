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
});
