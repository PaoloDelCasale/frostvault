import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configureApiClient, resetApiClientForTests } from "./client";
import {
  downloadAdminMetadataBackup,
  fetchAdminMetadataBackups,
  estimateAdminStorageCost,
  runAdminMetadataBackup,
  createVault,
  fetchI18nCatalog,
  fetchMe,
  fetchNotifications,
  fetchVaults,
  relocateAdminVault,
  requestScan,
  selectVault,
  markNotificationRead,
  markAllNotificationsRead,
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

  it("uses the notification inbox contracts", async () => {
    document.cookie = "frostvault_csrf=notification-csrf";
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({ items: [], unread_count: 3, has_more: false }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          items: [{ id: 1, read: false }],
          unread_count: 1,
          has_more: true,
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
      )
      .mockResolvedValueOnce(
        jsonResponse({ marked_count: 2, unread_count: 0 }),
      );

    await expect(fetchNotifications()).resolves.toMatchObject({
      items: [],
      unread_count: 3,
      has_more: false,
    });
    await expect(
      fetchNotifications({ status: "unread", limit: 25, beforeId: 99 }),
    ).resolves.toMatchObject({
      unread_count: 1,
      has_more: true,
    });
    await expect(markNotificationRead(10)).resolves.toMatchObject({
      id: 10,
      read: true,
    });
    await expect(markAllNotificationsRead()).resolves.toMatchObject({
      marked_count: 2,
      unread_count: 0,
    });

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/notifications",
      "/api/notifications?limit=25&status=unread&before_id=99",
      "/api/notifications/read",
      "/api/notifications/read-all",
    ]);
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toEqual({
      notification_id: 10,
    });
    for (const call of fetchMock.mock.calls.slice(2)) {
      expect(new Headers(call[1]?.headers).get("X-CSRF-Token")).toBe(
        "notification-csrf",
      );
    }
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
        region: "eu-south-1",
        pricing_effective_at: "2026-08-01T00:00:00+00:00",
        assumptions: {},
      }),
    );

    await expect(
      estimateAdminStorageCost({
        vault_id: 3,
        storage_class: "DEEP_ARCHIVE",
      }),
    ).resolves.toMatchObject({
      size_bytes: 1073741824,
      region: "eu-south-1",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/admin/cost-estimates/storage",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          vault_id: 3,
          storage_class: "DEEP_ARCHIVE",
        }),
      }),
    );
    expect(
      new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("X-CSRF-Token"),
    ).toBe("estimate-csrf");
  });

  it("vault creation and selection have no encryption or custody contract", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ id: 3, uuid: "u", slug: "docs", name: "Docs" }, 201))
      .mockResolvedValueOnce(jsonResponse({ vault_id: 3 }));
    await expect(createVault({ name: "Docs" })).resolves.toMatchObject({ id: 3 });
    await expect(selectVault({ vault_id: 3 })).resolves.toEqual({ vault_id: 3 });
    expect(fetchMock.mock.calls.map((call) => [call[0], (call[1] as RequestInit).method])).toEqual([
      ["/api/vaults", "POST"],
      ["/api/vaults/select", "POST"],
    ]);
  });
});
