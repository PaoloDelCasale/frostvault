import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configureApiClient, resetApiClientForTests } from "./client";
import {
  confirmFileRename,
  activateAdminCostPriceBook,
  confirmFolderRename,
  confirmRecoveryCustody,
  createAdminCostPriceBook,
  downloadAdminMetadataBackup,
  fetchAdminAuditEvents,
  fetchAdminMetadataBackups,
  estimateAdminStorageCost,
  fetchActiveAdminCostPriceBook,
  fetchAdminCostPriceBooks,
  runAdminMetadataBackup,
  createVault,
  exportRecoverySecret,
  fetchI18nCatalog,
  fetchMe,
  fetchNotificationPreferences,
  fetchNotifications,
  fetchRenameCandidates,
  fetchVaultAuditEvents,
  fetchVaults,
  relocateAdminVault,
  requestScan,
  saveAdminSmtpEndpoint,
  saveAdminWebhookEndpoint,
  selectVault,
  markNotificationRead,
  setNotificationPreference,
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

  it("uses current-Vault rename routes and shared CSRF handling", async () => {
    document.cookie = "frostvault_csrf=rename-csrf";
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(jsonResponse({ vault_file_id: "old-id" }, 202))
      .mockResolvedValueOnce(jsonResponse({ renamed_ids: ["old-id"] }, 202));

    await expect(fetchRenameCandidates()).resolves.toEqual({ items: [] });
    await confirmFileRename({
      vault_file_id: "00000000-0000-0000-0000-000000000001",
      new_path: "new/name.txt",
    });
    await confirmFolderRename({ old_prefix: "old", new_prefix: "new" });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/rename-candidates");
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe("GET");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/confirm-rename");
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        vault_file_id: "00000000-0000-0000-0000-000000000001",
        new_path: "new/name.txt",
      }),
    });
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/confirm-folder-rename");
    expect(fetchMock.mock.calls[2]?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ old_prefix: "old", new_prefix: "new" }),
    });
    for (const call of fetchMock.mock.calls.slice(1)) {
      expect(new Headers(call[1]?.headers).get("X-CSRF-Token")).toBe("rename-csrf");
    }
  });

  it("uses the notification inbox and personal preference contracts", async () => {
    document.cookie = "frostvault_csrf=notification-csrf";
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({ items: [], unread_count: 3 }),
      )
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(
        jsonResponse({
          id: 42,
          user_id: 7,
          vault_id: 9,
          event: "job_completed",
          channel: "in_app",
          enabled: true,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          id: 10,
          user_id: 7,
          vault_id: 9,
          event: "job_completed",
          title: "Completed",
          body: "archive.txt",
          read: true,
        }),
      );

    await expect(fetchNotifications()).resolves.toMatchObject({
      items: [],
      unread_count: 3,
    });
    await expect(fetchNotificationPreferences()).resolves.toMatchObject({
      items: [],
    });
    await expect(
      setNotificationPreference({
        event: "job_completed",
        channel: "in_app",
        enabled: true,
      }),
    ).resolves.toMatchObject({ enabled: true });
    await expect(markNotificationRead(10)).resolves.toMatchObject({
      id: 10,
      read: true,
    });

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/notifications",
      "/api/vault/notification-preferences",
      "/api/vault/notification-preferences",
      "/api/notifications/read",
    ]);
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toEqual({
      event: "job_completed",
      channel: "in_app",
      enabled: true,
    });
    expect(JSON.parse(String(fetchMock.mock.calls[3]?.[1]?.body))).toEqual({
      notification_id: 10,
    });
    for (const call of fetchMock.mock.calls.slice(2)) {
      expect(new Headers(call[1]?.headers).get("X-CSRF-Token")).toBe(
        "notification-csrf",
      );
    }
  });

  it("fetches the fixed newest audit windows without filter parameters", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ events: [{ id: 100 }] }))
      .mockResolvedValueOnce(jsonResponse({ events: [{ id: 99 }] }));

    await expect(fetchVaultAuditEvents()).resolves.toEqual({
      events: [{ id: 100 }],
    });
    await expect(fetchAdminAuditEvents()).resolves.toEqual({
      events: [{ id: 99 }],
    });

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/audit-events",
      "/api/admin/audit-events",
    ]);
  });

  it("requests a scan through the shared API client", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        message: "Scan of Test Archive started",
        message_key: "api.scan_started",
      }, 202),
    );

    await expect(requestScan()).resolves.toEqual({
      message: "Scan of Test Archive started",
      message_key: "api.scan_started",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/scan",
      expect.objectContaining({ method: "POST" }),
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

  it("metadata backup helpers use typed admin routes without exposing filesystem paths", async () => {
    configureApiClient({ csrfToken: "backup-csrf" });
    const run = {
      id: 4,
      created_at: "2026-07-01T00:00:00+00:00",
      finished_at: "2026-07-01T00:00:01+00:00",
      reason: "manual",
      backend: "sqlite",
      status: "succeeded",
      digest_sha256: "a".repeat(64),
      database_sha256: "b".repeat(64),
      local_path: "/data/backups/metadata-4.bak.enc",
      s3_key: "system/backups/metadata-4.bak.enc",
      size_bytes: 42,
      error_message: null,
      verified_at: null,
    };
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({
          status: {
            last_status: "succeeded",
            last_run: run,
            succeeded_count: 1,
            failed_count: 0,
          },
          runs: [run],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          ok: true,
          reason: "manual",
          path: "/data/backups/metadata-5.bak.enc",
          local_path: "/data/backups/metadata-5.bak.enc",
          digest_sha256: "c".repeat(64),
          database_sha256: "d".repeat(64),
          backend: "sqlite",
          s3_key: null,
          size_bytes: 43,
          filename: "metadata-5.bak.enc",
          created_at: "2026-07-01T00:00:02+00:00",
        }),
      );

    const listed = await fetchAdminMetadataBackups();
    expect(listed.runs[0]).toEqual({
      id: 4,
      created_at: run.created_at,
      finished_at: run.finished_at,
      reason: run.reason,
      backend: run.backend,
      status: run.status,
      digest_sha256: run.digest_sha256,
      database_sha256: run.database_sha256,
      s3_key: run.s3_key,
      size_bytes: run.size_bytes,
      error_message: null,
      verified_at: null,
    });
    expect(listed.runs[0]).not.toHaveProperty("local_path");
    expect(listed.status.last_run).not.toHaveProperty("local_path");

    const result = await runAdminMetadataBackup();
    expect(result).not.toHaveProperty("path");
    expect(result).not.toHaveProperty("local_path");
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      reason: "operator requested backup",
    });
    expect(
      new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get("X-CSRF-Token"),
    ).toBe("backup-csrf");
  });

  it("downloads metadata artifacts through the authenticated binary helper", async () => {
    const checksum = "e".repeat(64);
    fetchMock.mockResolvedValueOnce(
      new Response("ciphertext", {
        status: 200,
        headers: {
          "Content-Type": "application/octet-stream",
          "Content-Disposition": "attachment; filename*=UTF-8''metadata%20backup.bak.enc",
          "X-Checksum-SHA256": checksum.toUpperCase(),
        },
      }),
    );

    const artifact = await downloadAdminMetadataBackup(9);
    expect(artifact.filename).toBe("metadata backup.bak.enc");
    expect(artifact.checksumSha256).toBe(checksum);
    expect(await artifact.blob.text()).toBe("ciphertext");
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/admin/metadata-backups/download/9",
    );
    expect(
      new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("X-CSRF-Token"),
    ).toBeNull();
  });

  it("cost price book helpers use the admin routes and shared mutation protections", async () => {
    configureApiClient({ csrfToken: "cost-csrf" });
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(
        jsonResponse({
          id: null,
          name: "builtin-defaults",
          currency: "EUR",
          effective_at: "2026-01-01T00:00:00+00:00",
          updated_at: null,
          assumptions: { region: "eu-south-1" },
          storage_rates: { STANDARD: 0.023 },
          restore_rates: { GLACIER: { Bulk: 0.0025 } },
          is_active: true,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ id: 8, name: "July", is_active: false }, 201),
      )
      .mockResolvedValueOnce(
        jsonResponse({ id: 8, name: "July", is_active: true }),
      );

    await expect(fetchAdminCostPriceBooks()).resolves.toEqual({ items: [] });
    await expect(fetchActiveAdminCostPriceBook()).resolves.toMatchObject({
      id: null,
      is_active: true,
    });
    await createAdminCostPriceBook({
      name: "July",
      currency: "EUR",
      effective_at: "2026-07-01T00:00:00+00:00",
      assumptions: { region: "eu-south-1", note: "test" },
      storage_rates: { CUSTOM: 0.01 },
      restore_rates: { CUSTOM: { AnyTier: 0.02 } },
      reason: "update rates",
    });
    await activateAdminCostPriceBook(8, { reason: "activate rates" });

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/admin/cost-price-books",
      "/api/admin/cost-price-books/active",
      "/api/admin/cost-price-books",
      "/api/admin/cost-price-books/8/activate",
    ]);
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toMatchObject({
      assumptions: { region: "eu-south-1", note: "test" },
      storage_rates: { CUSTOM: 0.01 },
      restore_rates: { CUSTOM: { AnyTier: 0.02 } },
      reason: "update rates",
    });
    for (const call of fetchMock.mock.calls.slice(2)) {
      expect(new Headers(call[1]?.headers).get("X-CSRF-Token")).toBe("cost-csrf");
    }
  });

  it("configures the webhook endpoint through the admin mutation helper", async () => {
    configureApiClient({ csrfToken: "notification-csrf" });
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        id: 4,
        kind: "webhook",
        name: "global-webhook",
        enabled: true,
      }),
    );

    await expect(
      saveAdminWebhookEndpoint({
        url: "https://hooks.example.test/frostvault",
        enabled: true,
        reason: "configure outbound alerts",
      }),
    ).resolves.toMatchObject({ id: 4, kind: "webhook", enabled: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/admin/notification-endpoints/webhook",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          url: "https://hooks.example.test/frostvault",
          enabled: true,
          reason: "configure outbound alerts",
        }),
      }),
    );
    expect(
      new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("X-CSRF-Token"),
    ).toBe("notification-csrf");
  });

  it("replays SMTP endpoint configuration once after recent reauthentication", async () => {
    const requestPassword = vi.fn(async () => "reauth-password");
    const password = "smtp-write-only-secret";
    configureApiClient({
      csrfToken: "notification-csrf",
      getAuthMethod: () => "local",
      requestPassword,
    });
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ error: "reauth_required" }, 403))
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(
        jsonResponse({ id: 5, kind: "smtp", enabled: true }),
      );

    await expect(
      saveAdminSmtpEndpoint({
        host: "smtp.example.test",
        port: 587,
        username: "alerts",
        password,
        from_address: "alerts@example.com",
        use_tls: true,
        enabled: true,
        reason: "configure email alerts",
      }),
    ).resolves.toMatchObject({ id: 5, kind: "smtp", enabled: true });

    expect(requestPassword).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/admin/notification-endpoints/smtp",
      "/api/reauth",
      "/api/admin/notification-endpoints/smtp",
    ]);
    const replayBody = JSON.parse(
      String(fetchMock.mock.calls[2]?.[1]?.body),
    ) as Record<string, unknown>;
    expect(replayBody.password).toBe(password);
    for (const call of fetchMock.mock.calls) {
      expect(new Headers(call[1]?.headers).get("X-CSRF-Token")).toBe(
        "notification-csrf",
      );
    }
  });

  it("requests each admin storage estimate through the typed mutation helper", async () => {
    configureApiClient({ csrfToken: "estimate-csrf" });
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        kind: "storage_month",
        size_bytes: 1073741824,
        storage_class: "DEEP_ARCHIVE",
        tier: null,
        estimated_cost_eur: 0.00099,
        estimated_hours: null,
        currency: "EUR",
        price_book_id: 12,
        price_book_name: "August rates",
        pricing_effective_at: "2026-08-01T00:00:00+00:00",
        assumptions: {},
      }),
    );

    await expect(
      estimateAdminStorageCost({
        size_bytes: 1073741824,
        storage_class: "DEEP_ARCHIVE",
      }),
    ).resolves.toMatchObject({
      price_book_id: 12,
      price_book_name: "August rates",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/admin/cost-estimates/storage",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          size_bytes: 1073741824,
          storage_class: "DEEP_ARCHIVE",
        }),
      }),
    );
    expect(
      new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("X-CSRF-Token"),
    ).toBe("estimate-csrf");
  });

  it("activation keeps the shared local reauthentication retry behavior", async () => {
    const requestPassword = vi.fn(async () => "recent-password");
    configureApiClient({
      csrfToken: "cost-csrf",
      getAuthMethod: () => "local",
      requestPassword,
    });
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ error: "reauth_required" }, 403))
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ id: 8, is_active: true }));

    await expect(
      activateAdminCostPriceBook(8, { reason: "activate rates" }),
    ).resolves.toMatchObject({ id: 8, is_active: true });

    expect(requestPassword).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/admin/cost-price-books/8/activate",
      "/api/reauth",
      "/api/admin/cost-price-books/8/activate",
    ]);
    for (const call of fetchMock.mock.calls) {
      expect(new Headers(call[1]?.headers).get("X-CSRF-Token")).toBe("cost-csrf");
    }
  });

  it("creation keeps the shared local reauthentication retry behavior", async () => {
    const requestPassword = vi.fn(async () => "recent-password");
    configureApiClient({
      csrfToken: "cost-csrf",
      getAuthMethod: () => "local",
      requestPassword,
    });
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ error: "reauth_required" }, 403))
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ id: 9, is_active: false }, 201));

    await expect(
      createAdminCostPriceBook({
        name: "August",
        currency: "EUR",
        effective_at: "2026-08-01T00:00:00+00:00",
        assumptions: {},
        storage_rates: {},
        restore_rates: {},
        reason: "add August rates",
      }),
    ).resolves.toMatchObject({ id: 9, is_active: false });

    expect(requestPassword).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/admin/cost-price-books",
      "/api/reauth",
      "/api/admin/cost-price-books",
    ]);
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
