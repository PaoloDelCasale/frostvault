import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configureApiClient, resetApiClientForTests } from "./client";
import {
  confirmRecoveryCustody,
  createVault,
  exportRecoverySecret,
  fetchI18nCatalog,
  fetchMe,
  fetchVaults,
  relocateAdminVault,
  selectVault,
} from "./endpoints";

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("foundation endpoint helpers", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    configureApiClient({ fetch: fetchMock });
  });

  afterEach(() => {
    resetApiClientForTests();
  });

  it("fetchMe caches csrf_token and auth_method for later mutating calls", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({
          id: 1,
          username: "ada",
          display_name: "Ada",
          is_admin: false,
          active: true,
          session_version: 1,
          csrf_token: "from-me",
          auth_method: "local",
          locale: "en",
          locales: ["en", "it"],
          vault: null,
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ ok: true }));

    await fetchMe();
    document.cookie = "frostvault_csrf=cookie-should-not-win";
    const { apiRequest } = await import("./client");
    await apiRequest("/api/scan", { method: "POST", body: "{}" });

    expect(new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get("X-CSRF-Token")).toBe(
      "from-me",
    );
  });

  it("fetchVaults and fetchI18nCatalog hit the foundation routes", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(
        jsonResponse({
          locale: "en",
          locales: ["en", "it"],
          messages: { "api.locale_updated": "Locale updated." },
        }),
      );

    await expect(fetchVaults()).resolves.toEqual({ items: [] });
    const catalog = await fetchI18nCatalog("en");
    expect(catalog.messages["api.locale_updated"]).toBe("Locale updated.");
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/vaults");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/i18n/catalog?locale=en");
  });

  it("admin relocation sends only the constrained same-volume destination and reason", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        vault_id: 7,
        source_root: "/sources/photos/new-name",
        relocation_state: "scan_required",
        full_scan_required: true,
      }),
    );

    await relocateAdminVault(7, {
      volume_alias: "photos",
      relative_path: "new-name",
      reason: "operator renamed directory",
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/admin/vaults/7/relocate");
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        volume_alias: "photos",
        relative_path: "new-name",
        reason: "operator renamed directory",
      }),
    });
  });

  it("vault create and recovery helpers hit the agreed routes", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(
          {
            id: 3,
            uuid: "u",
            slug: "docs",
            name: "Docs",
            role: "owner",
            encryption_mode: "crypt",
            recovery_custody_confirmed: false,
            recovery_export: "export-body",
          },
          201,
        ),
      )
      .mockResolvedValueOnce(jsonResponse({ vault_id: 3 }))
      .mockResolvedValueOnce(
        jsonResponse({
          vault_id: 3,
          recovery_custody_confirmed: true,
          recovery_custody_confirmed_at: "2026-07-26T10:00:00Z",
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ recovery_export: "re-export" }));

    await expect(
      createVault({ name: "Docs", encryption_mode: "crypt" }),
    ).resolves.toMatchObject({ recovery_export: "export-body" });
    await expect(selectVault({ vault_id: 3 })).resolves.toEqual({ vault_id: 3 });
    await expect(confirmRecoveryCustody({ acknowledged: true })).resolves.toMatchObject({
      recovery_custody_confirmed: true,
    });
    await expect(
      exportRecoverySecret({ reason: "offline backup copy" }),
    ).resolves.toEqual({ recovery_export: "re-export" });

    expect(fetchMock.mock.calls.map((call) => [call[0], (call[1] as RequestInit).method])).toEqual([
      ["/api/vaults", "POST"],
      ["/api/vaults/select", "POST"],
      ["/api/vault/recovery/confirm", "POST"],
      ["/api/vault/recovery/export", "POST"],
    ]);
  });
});
