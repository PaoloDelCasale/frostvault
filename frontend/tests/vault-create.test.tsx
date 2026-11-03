import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { VaultCreatePage } from "@/pages/vault-create";

const locales = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app/locales");
const en = JSON.parse(readFileSync(path.join(locales, "en.json"), "utf8")) as Record<string, string>;
const itCatalog = JSON.parse(readFileSync(path.join(locales, "it.json"), "utf8")) as Record<string, string>;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("plaintext Vault creation", () => {
  const fetchMock = vi.fn();
  const navigate = vi.fn();
  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    navigate.mockReset();
    configureApiClient({ fetch: fetchMock, navigate });
    document.documentElement.lang = "en";
  });
  afterEach(() => { cleanup(); resetApiClientForTests(); });

  function renderPage() {
    return render(<ApiQueryProvider client={createAppQueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <I18nProvider><VaultCreatePage displayName="Ada" onNavigate={navigate} /></I18nProvider>
    </ApiQueryProvider>);
  }
  function requests({ adopt = false, fail = false, messages = en } = {}) {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) return Promise.resolve(json({ locale: messages === en ? "en" : "it", locales: ["en", "it"], messages }));
      if (url === "/api/admin/source-volumes") return Promise.resolve(json({ items: adopt ? [{ alias: "photos", health: "ok", access: "rw" }] : [] }));
      if (url.startsWith("/api/source-volumes/photos/browse")) return Promise.resolve(json({ volume_alias: "photos", relative_path: "albums", items: [{ name: "2024", relative_path: "albums/2024", navigable: true, selectable: true, occupation: null }] }));
      if (url === "/api/vaults" && init?.method === "POST") return Promise.resolve(fail
        ? json({ message_key: "ui.vault_create.failed", detail: "Slug taken" }, 422)
        : json({ id: 42, uuid: "vault-uuid", slug: "photos", name: "Photos", creation_mode: adopt ? "adopt" : "empty" }, 201));
      if (url === "/api/vaults/select") return Promise.resolve(json({ vault_id: 42 }));
      return Promise.reject(new Error(`Unexpected request ${url}`));
    });
  }
  function payload() {
    const call = fetchMock.mock.calls.find(([url, init]) => url === "/api/vaults" && init?.method === "POST");
    expect(call).toBeDefined();
    return JSON.parse(String(call![1].body)) as unknown;
  }

  it("creates and selects immediately, with no encryption choice or custody/export step", async () => {
    requests();
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: en["ui.vault_create.title"] });
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
    await user.type(screen.getByRole("textbox", { name: en["ui.vault_create.name"] }), "Photos");
    await user.type(screen.getByRole("textbox", { name: new RegExp(en["ui.vault_create.slug"], "i") }), "photos");
    await user.click(screen.getByRole("button", { name: en["ui.vault_create.submit"] }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/"));
    expect(payload()).toEqual({ name: "Photos", slug: "photos", creation_mode: "empty" });
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/recovery"))).toBe(false);
  });

  it("preserves localized validation errors and the form", async () => {
    requests({ fail: true, messages: itCatalog });
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: itCatalog["ui.vault_create.title"] });
    const name = screen.getByRole("textbox", { name: itCatalog["ui.vault_create.name"] });
    await user.type(name, "Photos");
    await user.click(screen.getByRole("button", { name: itCatalog["ui.vault_create.submit"] }));
    expect(await screen.findByRole("alert")).toHaveTextContent(itCatalog["ui.vault_create.failed"]);
    expect(name).toHaveValue("Photos");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("adopts a free Source Volume directory without a crypt contract", async () => {
    requests({ adopt: true });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("radio", { name: en["ui.vault_create.mode_adopt"] }));
    await screen.findByTestId("vault-create-adopt");
    await user.type(screen.getByRole("textbox", { name: en["ui.vault_create.name"] }), "Photos");
    const buttons = await screen.findAllByRole("button", { name: en["ui.source_directory_browser_select"] });
    await user.click(buttons[0]!);
    await user.click(screen.getByRole("button", { name: en["ui.vault_create.submit"] }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/"));
    expect(payload()).toEqual({ name: "Photos", creation_mode: "adopt", volume_alias: "photos", relative_path: "albums/2024" });
  });
});
