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
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
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

  function mockEmptySourceAreas() {
    return jsonResponse({ items: [] });
  }

  function withSourceAreas(
    impl: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>,
  ) {
    return (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/source-areas") {
        return Promise.resolve(mockEmptySourceAreas());
      }
      return impl(input, init);
    };
  }

  it("posts a valid creation payload then navigates to the new vault archive", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(
      withSourceAreas((input: RequestInfo | URL, init?: RequestInit) => {
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
              creation_mode: "empty",
            },
            201,
          ),
        );
      }
      if (url === "/api/vaults/select" && init?.method === "POST") {
        return Promise.resolve(jsonResponse({ vault_id: 42 }));
      }
      return Promise.reject(new Error(`unexpected request ${url}`));
    }),
    );

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
        creation_mode: "empty",
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
      if (url === "/api/source-areas") {
        return Promise.resolve(mockEmptySourceAreas());
      }
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

  it("displays the full recovery export returned by the API without altering it", async () => {
    const user = userEvent.setup();
    const recoveryExport = [
      "# FrostVault recovery export — keep offline",
      "[fv-crypt]",
      "type = crypt",
      "remote = frostvault:bucket/prefix",
      "password = very-long-obscured-password-token-aaaaaaaaaaaaaaaa",
      "password2 = very-long-obscured-salt-token-bbbbbbbbbbbbbbbb",
      "filename_encryption = standard",
      "directory_name_encryption = true",
    ].join("\n");

    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/source-areas") {
        return Promise.resolve(mockEmptySourceAreas());
      }
      if (url.startsWith("/api/i18n/catalog")) {
        return Promise.resolve(mockCatalog(en));
      }
      if (url === "/api/vaults" && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              id: 7,
              uuid: "crypt-uuid",
              slug: "secret",
              name: "Secret",
              role: "owner",
              encryption_mode: "crypt",
              recovery_custody_confirmed: false,
              recovery_export: recoveryExport,
            },
            201,
          ),
        );
      }
      return Promise.reject(new Error(`unexpected request ${url}`));
    });

    renderPage();
    await screen.findByRole("heading", { name: en["ui.vault_create.title"] });

    await user.type(
      screen.getByRole("textbox", { name: en["ui.vault_create.name"] }),
      "Secret",
    );
    await user.click(
      screen.getByRole("radio", { name: en["ui.vault_create.encryption_crypt"] }),
    );
    await user.click(
      screen.getByRole("button", { name: en["ui.vault_create.submit"] }),
    );

    expect(
      await screen.findByRole("heading", { name: en["ui.recovery.title"] }),
    ).toBeInTheDocument();
    const material = screen.getByTestId("recovery-export-material");
    expect(material).toHaveTextContent(recoveryExport.replace(/\n/g, " "));
    expect(material.textContent).toBe(recoveryExport);
    expect(document.querySelector("textarea")).toBeNull();
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("copies recovery material to the clipboard and downloads the complete file", async () => {
    const user = userEvent.setup();
    const recoveryExport = "line-one\nline-two\npassword = keep-me-whole";
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/source-areas") {
        return Promise.resolve(mockEmptySourceAreas());
      }
      if (url.startsWith("/api/i18n/catalog")) {
        return Promise.resolve(mockCatalog(en));
      }
      if (url === "/api/vaults" && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              id: 7,
              uuid: "crypt-uuid",
              slug: "secret",
              name: "Secret",
              role: "owner",
              encryption_mode: "crypt",
              recovery_custody_confirmed: false,
              recovery_export: recoveryExport,
            },
            201,
          ),
        );
      }
      return Promise.reject(new Error(`unexpected request ${url}`));
    });

    renderPage();
    await screen.findByRole("heading", { name: en["ui.vault_create.title"] });
    await user.type(
      screen.getByRole("textbox", { name: en["ui.vault_create.name"] }),
      "Secret",
    );
    await user.click(
      screen.getByRole("radio", { name: en["ui.vault_create.encryption_crypt"] }),
    );
    await user.click(
      screen.getByRole("button", { name: en["ui.vault_create.submit"] }),
    );
    await screen.findByRole("heading", { name: en["ui.recovery.title"] });

    await user.click(screen.getByRole("button", { name: en["ui.recovery.copy"] }));
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(recoveryExport);
    });

    const createObjectURL = vi.fn(() => "blob:recovery-export");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL,
      revokeObjectURL,
    });

    const anchorClick = vi.fn();
    const realCreateElement = document.createElement.bind(document);
    const createElSpy = vi
      .spyOn(document, "createElement")
      .mockImplementation((tagName: string, options?: ElementCreationOptions) => {
        const el = realCreateElement(tagName, options);
        if (tagName.toLowerCase() === "a") {
          el.click = anchorClick;
        }
        return el;
      });

    await user.click(screen.getByRole("button", { name: en["ui.recovery.download"] }));
    await waitFor(() => {
      expect(createObjectURL).toHaveBeenCalled();
    });
    const blob = createObjectURL.mock.calls[0][0] as Blob;
    expect(blob).toBeInstanceOf(Blob);
    const downloaded = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(blob);
    });
    expect(downloaded).toBe(recoveryExport);
    expect(anchorClick).toHaveBeenCalled();
    createElSpy.mockRestore();
  });

  it("confirms recovery custody only after an irreversibility dialog", async () => {
    const user = userEvent.setup();
    const recoveryExport = "keep-offline";
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/source-areas") {
        return Promise.resolve(mockEmptySourceAreas());
      }
      if (url.startsWith("/api/i18n/catalog")) {
        return Promise.resolve(mockCatalog(en));
      }
      if (url === "/api/vaults" && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              id: 9,
              uuid: "crypt-uuid",
              slug: "secret",
              name: "Secret",
              role: "owner",
              encryption_mode: "crypt",
              recovery_custody_confirmed: false,
              recovery_export: recoveryExport,
            },
            201,
          ),
        );
      }
      if (url === "/api/vaults/select" && init?.method === "POST") {
        return Promise.resolve(jsonResponse({ vault_id: 9 }));
      }
      if (url === "/api/vault/recovery/confirm" && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse({
            vault_id: 9,
            recovery_custody_confirmed: true,
            recovery_custody_confirmed_at: "2026-07-26T10:00:00Z",
          }),
        );
      }
      return Promise.reject(new Error(`unexpected request ${url}`));
    });

    renderPage();
    await screen.findByRole("heading", { name: en["ui.vault_create.title"] });
    await user.type(
      screen.getByRole("textbox", { name: en["ui.vault_create.name"] }),
      "Secret",
    );
    await user.click(
      screen.getByRole("radio", { name: en["ui.vault_create.encryption_crypt"] }),
    );
    await user.click(
      screen.getByRole("button", { name: en["ui.vault_create.submit"] }),
    );
    await screen.findByRole("heading", { name: en["ui.recovery.title"] });

    await user.click(screen.getByRole("button", { name: en["ui.recovery.confirm"] }));
    expect(await screen.findByRole("alertdialog")).toHaveTextContent(
      en["ui.recovery.confirm_description"],
    );
    expect(
      fetchMock.mock.calls.some(([url]) => String(url) === "/api/vault/recovery/confirm"),
    ).toBe(false);

    await user.click(screen.getByRole("button", { name: en["ui.vault_create.cancel"] }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
    expect(
      fetchMock.mock.calls.some(([url]) => String(url) === "/api/vault/recovery/confirm"),
    ).toBe(false);

    await user.click(screen.getByRole("button", { name: en["ui.recovery.confirm"] }));
    await screen.findByRole("alertdialog");
    await user.click(
      screen.getByRole("button", { name: en["ui.recovery.confirm_action"] }),
    );

    await waitFor(() => {
      const confirmCall = fetchMock.mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/vault/recovery/confirm" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
      expect(confirmCall).toBeDefined();
      expect(JSON.parse(String((confirmCall![1] as RequestInit).body))).toEqual({
        acknowledged: true,
      });
    });
    await waitFor(() => {
      expect(navigateMock).toHaveBeenCalledWith("/");
    });
  });

  it("keeps the custody warning visible until custody is confirmed", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/source-areas") {
        return Promise.resolve(mockEmptySourceAreas());
      }
      if (url.startsWith("/api/i18n/catalog")) {
        return Promise.resolve(mockCatalog(en));
      }
      if (url === "/api/vaults" && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              id: 11,
              uuid: "crypt-uuid",
              slug: "secret",
              name: "Secret",
              role: "owner",
              encryption_mode: "crypt",
              recovery_custody_confirmed: false,
              recovery_export: "material",
            },
            201,
          ),
        );
      }
      if (url === "/api/vaults/select" && init?.method === "POST") {
        return Promise.resolve(jsonResponse({ vault_id: 11 }));
      }
      if (url === "/api/vault/recovery/confirm" && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse({
            vault_id: 11,
            recovery_custody_confirmed: true,
            recovery_custody_confirmed_at: "2026-07-26T10:00:00Z",
          }),
        );
      }
      return Promise.reject(new Error(`unexpected request ${url}`));
    });

    renderPage();
    await screen.findByRole("heading", { name: en["ui.vault_create.title"] });
    await user.type(
      screen.getByRole("textbox", { name: en["ui.vault_create.name"] }),
      "Secret",
    );
    await user.click(
      screen.getByRole("radio", { name: en["ui.vault_create.encryption_crypt"] }),
    );
    await user.click(
      screen.getByRole("button", { name: en["ui.vault_create.submit"] }),
    );
    await screen.findByRole("heading", { name: en["ui.recovery.title"] });

    expect(screen.getByTestId("recovery-custody-warning")).toHaveTextContent(
      en["ui.recovery.warning"],
    );

    await user.click(screen.getByRole("button", { name: en["ui.recovery.confirm"] }));
    await screen.findByRole("alertdialog");
    await user.click(screen.getByRole("button", { name: en["ui.vault_create.cancel"] }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
    expect(screen.getByTestId("recovery-custody-warning")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: en["ui.recovery.confirm"] }));
    await screen.findByRole("alertdialog");
    await user.click(
      screen.getByRole("button", { name: en["ui.recovery.confirm_action"] }),
    );

    await waitFor(() => {
      expect(screen.queryByTestId("recovery-custody-warning")).not.toBeInTheDocument();
    });
  });

  it("adopts an existing Source Area directory and stays usable at 375px", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) {
        return Promise.resolve(mockCatalog(en));
      }
      if (url === "/api/source-areas") {
        return Promise.resolve(
          jsonResponse({
            items: [
              {
                id: 1,
                user_id: 1,
                volume_alias: "photos",
                relative_path: "albums",
                created_at: "2026-07-28T00:00:00Z",
                availability: "available",
                usable: true,
              },
            ],
          }),
        );
      }
      if (url.startsWith("/api/source-volumes/photos/browse")) {
        return Promise.resolve(
          jsonResponse({
            volume_alias: "photos",
            relative_path: "albums",
            items: [
              {
                name: "2024",
                relative_path: "albums/2024",
                navigable: true,
                selectable: true,
                occupation: null,
              },
            ],
          }),
        );
      }
      if (url === "/api/vaults" && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              id: 77,
              uuid: "adopt-uuid",
              slug: "albums-archive",
              name: "Albums Archive",
              role: "owner",
              encryption_mode: "plain",
              recovery_custody_confirmed: true,
              creation_mode: "adopt",
            },
            201,
          ),
        );
      }
      if (url === "/api/vaults/select" && init?.method === "POST") {
        return Promise.resolve(jsonResponse({ vault_id: 77 }));
      }
      return Promise.reject(new Error(`unexpected request ${url}`));
    });

    const { container } = renderPage();
    await screen.findByRole("heading", { name: en["ui.vault_create.title"] });
    await waitFor(() => {
      expect(
        screen.getByRole("radio", { name: en["ui.vault_create.mode_adopt"] }),
      ).toBeInTheDocument();
    });

    Object.defineProperty(container.firstElementChild, "clientWidth", {
      configurable: true,
      value: 375,
    });

    await user.click(
      screen.getByRole("radio", { name: en["ui.vault_create.mode_adopt"] }),
    );
    await screen.findByTestId("vault-create-adopt");
    const adoptPanel = screen.getByTestId("vault-create-adopt");
    expect(adoptPanel.getBoundingClientRect().width).toBeLessThanOrEqual(440);

    await user.type(
      screen.getByRole("textbox", { name: en["ui.vault_create.name"] }),
      "Albums Archive",
    );

    const selectButtons = await screen.findAllByRole("button", {
      name: en["ui.source_area_browser_select"],
    });
    await user.click(selectButtons[0]!);

    await user.click(
      screen.getByRole("button", { name: en["ui.vault_create.submit"] }),
    );

    await waitFor(() => {
      const createCall = fetchMock.mock.calls.find(
        ([reqUrl, init]) =>
          String(reqUrl) === "/api/vaults" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
      expect(createCall).toBeDefined();
      expect(JSON.parse(String((createCall![1] as RequestInit).body))).toEqual({
        name: "Albums Archive",
        encryption_mode: "plain",
        creation_mode: "adopt",
        volume_alias: "photos",
        relative_path: "albums/2024",
      });
    });

    await waitFor(() => {
      expect(navigateMock).toHaveBeenCalledWith("/");
    });
  });
});
