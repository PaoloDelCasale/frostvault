import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
  type ApiFetch,
} from "@/api";
import { AccountPreferencesMenu } from "@/components/AccountPreferencesMenu";
import { NotificationCenter } from "@/components/NotificationCenter";
import { NotificationPreferencesPanel } from "@/components/NotificationPreferencesPanel";
import { I18nContext, type I18nContextValue } from "@/i18n/context";
import { translate } from "@/i18n/translate";
import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities, ShellNavHandlers } from "@/layout/types";
import { ThemeProvider } from "@/theme";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);

function loadCatalog(locale: "en" | "it"): Record<string, string> {
  return JSON.parse(
    readFileSync(path.join(localesDir, `${locale}.json`), "utf8"),
  ) as Record<string, string>;
}

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function requestUrl(call: unknown[]): string {
  return String(call[0] ?? "");
}

function requestMethod(init?: RequestInit): string {
  return String(init?.method ?? "GET").toUpperCase();
}

const shellCapabilities: ShellCapabilities = {
  vaultName: "Test Archive",
  isVaultOwner: true,
  canOperate: true,
  isAdmin: true,
  locale: "en",
  locales: ["en", "it"],
  vaults: [
    { id: 9, slug: "test", name: "Test Archive", role: "owner" },
    { id: 12, slug: "other", name: "Other Vault", role: "operator" },
  ],
  currentVaultId: 9,
};

const shellHandlers: ShellNavHandlers = {
  onNewVault: vi.fn(),
  onManageAccess: vi.fn(),
  onAdministration: vi.fn(),
  onSignOut: vi.fn(),
  onLocaleChange: vi.fn(),
  onVaultChange: vi.fn(),
};

function renderWithProviders(
  ui: ReactNode,
  locale: "en" | "it" = "en",
) {
  const catalog = loadCatalog(locale);
  const value: I18nContextValue = {
    locale,
    locales: ["en", "it"],
    ready: true,
    t: (key, params) => translate(catalog, key, params),
    setLocale: async () => undefined,
  };
  const client = createAppQueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    catalog,
    client,
    user: userEvent.setup(),
    ...render(
      <ApiQueryProvider client={client}>
        <I18nContext.Provider value={value}>
          <ThemeProvider>{ui}</ThemeProvider>
        </I18nContext.Provider>
      </ApiQueryProvider>,
    ),
  };
}

function renderPreferencesPanel(
  fetchMock: ApiFetch,
  overrides: Partial<{
    currentVaultId: number;
    vaultName: string;
    vaults: ShellCapabilities["vaults"];
    onVaultChange: (vaultId: number) => void;
    open: boolean;
    locale: "en" | "it";
  }> = {},
) {
  configureApiClient({ fetch: fetchMock, csrfToken: "notification-csrf" });
  const catalog = loadCatalog(overrides.locale ?? "en");
  const t = (key: string, params?: Record<string, unknown>) =>
    translate(catalog, key, params);
  const client = createAppQueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    catalog,
    client,
    user: userEvent.setup(),
    ...render(
      <NotificationPreferencesPanel
        open={overrides.open ?? true}
        onOpenChange={() => undefined}
        currentVaultId={overrides.currentVaultId ?? 9}
        vaultName={overrides.vaultName ?? "Test Archive"}
        vaults={
          overrides.vaults ?? [
            { id: 9, slug: "test", name: "Test Archive", role: "owner" },
          ]
        }
        onVaultChange={overrides.onVaultChange}
        locale={overrides.locale ?? "en"}
        t={t}
        queryClient={client}
      />,
    ),
  };
}

describe("notification preferences surface (#226)", () => {
  beforeEach(() => {
    resetApiClientForTests();
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
    resetApiClientForTests();
  });

  it("defaults absent in-app preferences to enabled and saves a first-click opt-out", async () => {
    let items = [
      {
        id: 2,
        user_id: 7,
        vault_id: 9,
        event: "job_completed",
        channel: "push",
        enabled: true,
      },
    ];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = requestMethod(init);
      if (url === "/api/vault/notification-preferences" && method === "GET") {
        return jsonResponse({ items });
      }
      if (url === "/api/vault/notification-preferences" && method === "POST") {
        const payload = JSON.parse(String(init?.body)) as {
          event: string;
          channel: string;
          enabled: boolean;
        };
        const saved = {
          id: 3,
          user_id: 7,
          vault_id: 9,
          event: payload.event,
          channel: payload.channel,
          enabled: payload.enabled,
        };
        const existing = items.findIndex(
          (item) =>
            item.event === saved.event && item.channel === saved.channel,
        );
        if (existing >= 0) {
          items = items.map((item, index) =>
            index === existing ? saved : item,
          );
        } else {
          items = [...items, saved];
        }
        return jsonResponse(saved);
      }
      throw new Error(`unexpected ${method} ${url}`);
    });

    const { user, catalog } = renderPreferencesPanel(fetchMock);
    const dialog = await screen.findByRole("dialog", {
      name: catalog["ui.notifications_preferences_heading"],
    });
    expect(
      within(dialog).getByTestId("notification-preferences-vault-name"),
    ).toHaveTextContent("Test Archive");

    const inApp = await within(dialog).findByRole("checkbox", {
      name: "Completed jobs: In-app",
    });
    expect(inApp).toBeChecked();
    expect(
      within(dialog).getByRole("checkbox", { name: "Completed jobs: Push" }),
    ).toBeChecked();
    expect(
      within(dialog).getByRole("checkbox", { name: "Failed jobs: In-app" }),
    ).toBeChecked();
    expect(
      within(dialog).getByRole("checkbox", { name: "Failed jobs: Push" }),
    ).toBeChecked();

    await user.click(inApp);
    expect(inApp).not.toBeChecked();
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          (call) =>
            requestUrl(call) === "/api/vault/notification-preferences" &&
            requestMethod(call[1] as RequestInit | undefined) === "POST",
        ),
      ).toBe(true);
    });
    const preferencePost = fetchMock.mock.calls.find(
      (call) =>
        requestUrl(call) === "/api/vault/notification-preferences" &&
        requestMethod(call[1] as RequestInit | undefined) === "POST",
    );
    expect(JSON.parse(String(preferencePost?.[1]?.body))).toEqual({
      event: "job_completed",
      channel: "in_app",
      enabled: false,
    });
  });

  it("keeps preference loading and error states independent of the inbox", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/notifications")) {
        return jsonResponse({
          unread_count: 1,
          has_more: false,
          items: [
            {
              id: 10,
              user_id: 7,
              vault_id: 9,
              job_id: 1,
              event: "job_completed",
              title: "Inbox still works",
              body: "body",
              title_key: null,
              body_key: null,
              message_params: {},
              in_app_enabled: true,
              dedupe_key: "job:10",
              created_at: "2025-01-01T10:00:00Z",
              read: false,
              read_at: null,
            },
          ],
        });
      }
      if (url === "/api/vault/notification-preferences") {
        return new Response("nope", { status: 500 });
      }
      throw new Error(`unexpected ${url}`);
    });

    configureApiClient({ fetch: fetchMock, csrfToken: "notification-csrf" });
    const catalog = loadCatalog("en");
    const client = createAppQueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const user = userEvent.setup();

    function Harness() {
      const [prefsOpen, setPrefsOpen] = useState(false);
      return (
        <ApiQueryProvider client={client}>
          <NotificationCenter
            t={(key, params) => translate(catalog, key, params)}
            queryClient={client}
            onOpenPreferences={() => setPrefsOpen(true)}
          />
          <NotificationPreferencesPanel
            open={prefsOpen}
            onOpenChange={setPrefsOpen}
            currentVaultId={9}
            vaultName="Test Archive"
            vaults={shellCapabilities.vaults}
            t={(key, params) => translate(catalog, key, params)}
            queryClient={client}
          />
        </ApiQueryProvider>
      );
    }

    render(<Harness />);

    await user.click(
      await screen.findByRole("button", {
        name: "Open notifications (1 unread)",
      }),
    );
    const inbox = await screen.findByRole("dialog", { name: "Notifications" });
    expect(within(inbox).getByText("Inbox still works")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.map((call) => requestUrl(call)).some(
        (url) => url === "/api/vault/notification-preferences",
      ),
    ).toBe(false);

    await user.click(
      within(inbox).getByRole("button", {
        name: catalog["ui.notifications_preferences_heading"],
      }),
    );
    const prefs = await screen.findByRole("dialog", {
      name: catalog["ui.notifications_preferences_heading"],
    });
    expect(
      await within(prefs).findByTestId("notification-preferences-error"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some((call) =>
        requestUrl(call).startsWith("/api/notifications"),
      ),
    ).toBe(true);
  });

  it("exposes preferences from the account menu with unambiguous Vault context", async () => {
    const catalog = loadCatalog("en");
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/notifications")) {
        return jsonResponse({ unread_count: 0, has_more: false, items: [] });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      throw new Error(`unexpected ${url}`);
    });
    configureApiClient({ fetch: fetchMock, csrfToken: "notification-csrf" });

    const { user } = renderWithProviders(
      <AppShell
        capabilities={shellCapabilities}
        handlers={shellHandlers}
        t={(key, params) => translate(catalog, key, params)}
      >
        <p>Archive content</p>
      </AppShell>,
    );

    await user.click(
      screen.getByRole("button", { name: catalog["ui.open_account_menu"] }),
    );
    const account = await screen.findByRole("dialog", {
      name: catalog["ui.account_menu"],
    });
    await user.click(
      within(account).getByRole("button", {
        name: catalog["ui.notifications_preferences_heading"],
      }),
    );

    const prefs = await screen.findByRole("dialog", {
      name: catalog["ui.notifications_preferences_heading"],
    });
    expect(
      within(prefs).getByTestId("notification-preferences"),
    ).toHaveAttribute("data-vault-id", "9");
    expect(
      within(prefs).getByRole("combobox", { name: catalog["ui.vault"] }),
    ).toHaveDisplayValue("Test Archive");
    expect(
      within(prefs).getByRole("checkbox", { name: "Completed jobs: In-app" }),
    ).toBeInTheDocument();
    expect(within(prefs).getAllByRole("checkbox")).toHaveLength(4);

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.map((call) => requestUrl(call)),
      ).toContain("/api/vault/notification-preferences");
    });
  });

  it("does not fetch preferences until the dedicated surface opens", async () => {
    const catalog = loadCatalog("en");
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/notifications")) {
        return jsonResponse({ unread_count: 0, has_more: false, items: [] });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      throw new Error(`unexpected ${url}`);
    });
    configureApiClient({ fetch: fetchMock, csrfToken: "notification-csrf" });

    const { user } = renderWithProviders(
      <AppShell
        capabilities={shellCapabilities}
        handlers={shellHandlers}
        t={(key, params) => translate(catalog, key, params)}
      >
        <p>Archive content</p>
      </AppShell>,
    );

    await user.click(
      screen.getByRole("button", { name: "Open notifications" }),
    );
    await screen.findByRole("dialog", { name: "Notifications" });
    expect(
      fetchMock.mock.calls.map((call) => requestUrl(call)).some(
        (url) => url === "/api/vault/notification-preferences",
      ),
    ).toBe(false);

    await user.keyboard("{Escape}");
    await user.click(
      screen.getByRole("button", { name: catalog["ui.open_account_menu"] }),
    );
    expect(
      fetchMock.mock.calls.map((call) => requestUrl(call)).some(
        (url) => url === "/api/vault/notification-preferences",
      ),
    ).toBe(false);
  });

  it("rolls back a failed preference save", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = requestMethod(init);
      if (url === "/api/vault/notification-preferences" && method === "GET") {
        return jsonResponse({
          items: [
            {
              id: 1,
              user_id: 7,
              vault_id: 9,
              event: "job_failed",
              channel: "push",
              enabled: true,
            },
          ],
        });
      }
      if (url === "/api/vault/notification-preferences" && method === "POST") {
        return new Response("nope", { status: 500 });
      }
      throw new Error(`unexpected ${method} ${url}`);
    });

    const { user, catalog } = renderPreferencesPanel(fetchMock);
    const dialog = await screen.findByRole("dialog", {
      name: catalog["ui.notifications_preferences_heading"],
    });
    const push = await within(dialog).findByRole("checkbox", {
      name: "Failed jobs: Push",
    });
    expect(push).toBeChecked();
    await user.click(push);
    expect(
      await within(dialog).findByRole("alert"),
    ).toHaveTextContent(catalog["ui.notifications_preferences_save_failed"]);
    expect(push).toBeChecked();
  });

  it("localizes the preferences surface in Italian", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      throw new Error(String(input));
    });
    const { catalog } = renderPreferencesPanel(fetchMock, { locale: "it" });
    const dialog = await screen.findByRole("dialog", {
      name: catalog["ui.notifications_preferences_heading"],
    });
    expect(
      await within(dialog).findByRole("checkbox", {
        name: `${catalog["ui.notifications_preference_job_completed"]}: ${catalog["ui.notifications_channel_in_app"]}`,
      }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(catalog["ui.notifications_preferences_vault_heading"]),
    ).toBeInTheDocument();
  });

  it("keeps 44px targets and works at a 320px-wide account entry", async () => {
    const catalog = loadCatalog("en");
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/notifications")) {
        return jsonResponse({ unread_count: 0, has_more: false, items: [] });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      throw new Error(url);
    });
    configureApiClient({ fetch: fetchMock, csrfToken: "notification-csrf" });

    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 320,
    });

    const { user } = renderWithProviders(
      <div style={{ width: 320 }}>
        <AccountPreferencesMenu
          locale="en"
          locales={["en", "it"]}
          isAdmin={false}
          handlers={{
            onNewVault: vi.fn(),
            onSignOut: vi.fn(),
            onLocaleChange: vi.fn(),
            onVaultChange: vi.fn(),
          }}
          notificationPreferences={{
            currentVaultId: 9,
            vaultName: "Test Archive",
            vaults: shellCapabilities.vaults,
            queryClient: createAppQueryClient({
              defaultOptions: { queries: { retry: false } },
            }),
          }}
          t={(key, params) => translate(catalog, key, params)}
        />
      </div>,
    );

    const trigger = screen.getByTestId("account-preferences-trigger");
    expect(trigger.className).toMatch(/min-h-11/);
    await user.click(trigger);
    const account = await screen.findByRole("dialog", {
      name: catalog["ui.account_menu"],
    });
    const prefsButton = within(account).getByTestId(
      "account-notification-preferences",
    );
    expect(prefsButton.className).toMatch(/min-h-11/);
    await user.click(prefsButton);
    const prefs = await screen.findByRole("dialog", {
      name: catalog["ui.notifications_preferences_heading"],
    });
    const checkbox = await within(prefs).findByRole("checkbox", {
      name: "Completed jobs: In-app",
    });
    expect(checkbox.closest("label")?.className).toMatch(/min-h-11/);
  });

  it("blocks edits when loaded preference rows belong to another Vault", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/vault/notification-preferences") {
        return jsonResponse({
          items: [
            {
              id: 1,
              user_id: 7,
              vault_id: 99,
              event: "job_completed",
              channel: "in_app",
              enabled: false,
            },
          ],
        });
      }
      throw new Error(String(input));
    });
    const { catalog } = renderPreferencesPanel(fetchMock, {
      currentVaultId: 9,
    });
    const dialog = await screen.findByRole("dialog", {
      name: catalog["ui.notifications_preferences_heading"],
    });
    const checkbox = await within(dialog).findByRole("checkbox", {
      name: "Completed jobs: In-app",
    });
    expect(checkbox).toBeDisabled();
    expect(
      within(dialog).getByText(
        catalog["ui.notifications_preferences_vault_mismatch"],
      ),
    ).toBeInTheDocument();
  });

  it("disables Vault switching while a preference save is in flight and re-enables after settle", async () => {
    const pendingSave = deferred<Response>();
    const onVaultChange = vi.fn();
    const multiVaults = [
      { id: 9, slug: "test", name: "Test Archive", role: "owner" as const },
      { id: 12, slug: "other", name: "Other Vault", role: "operator" as const },
    ];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = requestMethod(init);
      if (url === "/api/vault/notification-preferences" && method === "GET") {
        return jsonResponse({
          items: [
            {
              id: 1,
              user_id: 7,
              vault_id: 9,
              event: "job_completed",
              channel: "in_app",
              enabled: true,
            },
          ],
        });
      }
      if (url === "/api/vault/notification-preferences" && method === "POST") {
        return pendingSave.promise;
      }
      throw new Error(`unexpected ${method} ${url}`);
    });

    const { user, catalog } = renderPreferencesPanel(fetchMock, {
      currentVaultId: 9,
      vaults: multiVaults,
      onVaultChange,
    });
    const dialog = await screen.findByRole("dialog", {
      name: catalog["ui.notifications_preferences_heading"],
    });
    const vaultSwitcher = within(dialog).getByRole("combobox", {
      name: catalog["ui.vault"],
    });
    expect(vaultSwitcher).toBeEnabled();

    // Idle multi-Vault switching still works before any save starts.
    await user.selectOptions(vaultSwitcher, "12");
    expect(onVaultChange).toHaveBeenCalledWith(12);
    onVaultChange.mockClear();

    const checkbox = await within(dialog).findByRole("checkbox", {
      name: "Completed jobs: In-app",
    });
    await user.click(checkbox);

    await waitFor(() => {
      expect(vaultSwitcher).toBeDisabled();
    });
    // Disabled switcher must not issue a selection while the POST is pending.
    await user.selectOptions(vaultSwitcher, "12").catch(() => undefined);
    expect(onVaultChange).not.toHaveBeenCalled();
    expect(
      fetchMock.mock.calls.filter(
        (call) =>
          requestUrl(call) === "/api/vault/notification-preferences" &&
          requestMethod(call[1] as RequestInit | undefined) === "POST",
      ),
    ).toHaveLength(1);

    pendingSave.resolve(
      jsonResponse({
        id: 1,
        user_id: 7,
        vault_id: 9,
        event: "job_completed",
        channel: "in_app",
        enabled: false,
      }),
    );

    await waitFor(() => {
      expect(vaultSwitcher).toBeEnabled();
    });
    await user.selectOptions(vaultSwitcher, "12");
    expect(onVaultChange).toHaveBeenCalledWith(12);
  });

  it("re-enables Vault switching after a failed preference save", async () => {
    const pendingSave = deferred<Response>();
    const onVaultChange = vi.fn();
    const multiVaults = [
      { id: 9, slug: "test", name: "Test Archive", role: "owner" as const },
      { id: 12, slug: "other", name: "Other Vault", role: "operator" as const },
    ];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = requestMethod(init);
      if (url === "/api/vault/notification-preferences" && method === "GET") {
        return jsonResponse({
          items: [
            {
              id: 1,
              user_id: 7,
              vault_id: 9,
              event: "job_failed",
              channel: "push",
              enabled: true,
            },
          ],
        });
      }
      if (url === "/api/vault/notification-preferences" && method === "POST") {
        return pendingSave.promise;
      }
      throw new Error(`unexpected ${method} ${url}`);
    });

    const { user, catalog } = renderPreferencesPanel(fetchMock, {
      currentVaultId: 9,
      vaults: multiVaults,
      onVaultChange,
    });
    const dialog = await screen.findByRole("dialog", {
      name: catalog["ui.notifications_preferences_heading"],
    });
    const vaultSwitcher = within(dialog).getByRole("combobox", {
      name: catalog["ui.vault"],
    });
    const push = await within(dialog).findByRole("checkbox", {
      name: "Failed jobs: Push",
    });
    await user.click(push);

    await waitFor(() => {
      expect(vaultSwitcher).toBeDisabled();
    });

    pendingSave.resolve(new Response("nope", { status: 500 }));

    await waitFor(() => {
      expect(vaultSwitcher).toBeEnabled();
    });
    expect(
      await within(dialog).findByRole("alert"),
    ).toHaveTextContent(catalog["ui.notifications_preferences_save_failed"]);
    await user.selectOptions(vaultSwitcher, "12");
    expect(onVaultChange).toHaveBeenCalledWith(12);
  });
});
