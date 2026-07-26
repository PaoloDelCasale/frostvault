import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { VaultCreatePage } from "@/pages/vault-create";

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

describe("VaultCreatePage", () => {
  const fetchMock = vi.fn();
  const navigateMock = vi.fn();
  const en = loadCatalog("en");

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    navigateMock.mockReset();
    configureApiClient({ fetch: fetchMock, navigate: navigateMock });
    document.documentElement.lang = "en";
  });

  afterEach(() => {
    cleanup();
    resetApiClientForTests();
  });

  function renderPage(displayName = "Ada Lovelace") {
    const client = createAppQueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <ApiQueryProvider client={client}>
        <I18nProvider>
          <VaultCreatePage displayName={displayName} onNavigate={navigateMock} />
        </I18nProvider>
      </ApiQueryProvider>,
    );
  }

  function mockCatalog(messages: Record<string, string>, locale = "en") {
    return jsonResponse({ locale, locales: ["en", "it"], messages });
  }

  it("posts a valid creation payload then navigates to the new vault archive", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) {
        return Promise.resolve(mockCatalog(en));
      }
      if (url === "/api/vaults" && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              id: 42,
              uuid: "vault-uuid",
              slug: "family-photos",
              name: "Family Photos",
              role: "owner",
              encryption_mode: "plain",
              recovery_custody_confirmed: true,
            },
            201,
          ),
        );
      }
      if (url === "/api/vaults/select" && init?.method === "POST") {
        return Promise.resolve(jsonResponse({ vault_id: 42 }));
      }
      return Promise.reject(new Error(`unexpected request ${url}`));
    });

    renderPage();
    await screen.findByRole("heading", { name: en["ui.vault_create.title"] });

    await user.type(
      screen.getByRole("textbox", { name: en["ui.vault_create.name"] }),
      "Family Photos",
    );
    await user.type(
      screen.getByRole("textbox", { name: new RegExp(en["ui.vault_create.slug"], "i") }),
      "family-photos",
    );
    await user.click(
      screen.getByRole("button", { name: en["ui.vault_create.submit"] }),
    );

    await waitFor(() => {
      const createCall = fetchMock.mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/vaults" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
      expect(createCall).toBeDefined();
      expect(JSON.parse(String((createCall![1] as RequestInit).body))).toEqual({
        name: "Family Photos",
        slug: "family-photos",
        encryption_mode: "plain",
      });
    });

    await waitFor(() => {
      const selectCall = fetchMock.mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/vaults/select" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
      expect(selectCall).toBeDefined();
      expect(JSON.parse(String((selectCall![1] as RequestInit).body))).toEqual({
        vault_id: 42,
      });
    });

    await waitFor(() => {
      expect(navigateMock).toHaveBeenCalledWith("/");
    });
  });

  it("shows a localized validation error and preserves form contents", async () => {
    const user = userEvent.setup();
    const it = loadCatalog("it");
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) {
        return Promise.resolve(mockCatalog(it, "it"));
      }
      if (url === "/api/vaults" && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              message_key: "ui.vault_create.failed",
              detail: "Slug is already taken",
            },
            422,
          ),
        );
      }
      return Promise.reject(new Error(`unexpected request ${url}`));
    });

    renderPage();
    await screen.findByRole("heading", { name: it["ui.vault_create.title"] });

    const nameInput = screen.getByRole("textbox", {
      name: it["ui.vault_create.name"],
    });
    const slugInput = screen.getByRole("textbox", {
      name: new RegExp(it["ui.vault_create.slug"], "i"),
    });
    await user.type(nameInput, "Documents");
    await user.type(slugInput, "taken-slug");
    await user.click(
      screen.getByRole("button", { name: it["ui.vault_create.submit"] }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      it["ui.vault_create.failed"],
    );
    expect(nameInput).toHaveValue("Documents");
    expect(slugInput).toHaveValue("taken-slug");
    expect(navigateMock).not.toHaveBeenCalled();
  });
});
