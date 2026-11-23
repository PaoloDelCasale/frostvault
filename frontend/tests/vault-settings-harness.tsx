import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi, type Mock } from "vitest";

import { ApiQueryProvider, configureApiClient, resetApiClientForTests, type ApiFetch } from "@/api";
import { ToastRegion } from "@/components/Toast";
import { I18nProvider } from "@/i18n/I18nProvider";
import { VaultSettingsPanel } from "@/pages/vault-settings/VaultSettingsPanel";
import { jsonResponse } from "./support/http";
import { createTestQueryClient } from "./support/queryClient";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);

export function loadCatalog(locale: "en" | "it" = "en"): Record<string, string> {
  const raw = readFileSync(path.join(localesDir, `${locale}.json`), "utf8");
  return JSON.parse(raw) as Record<string, string>;
}

export const defaultPolicy = {
  auto_upload: true,
  local_release_after_days: null,
  stability_seconds: 300,
  include_globs: [] as string[],
  exclude_globs: [] as string[],
};

export const defaultDuplicateCleanup = {
  enabled: false,
  grace_days: 7,
  cap_files: 5000,
  cap_bytes: 50 * 1024 ** 3,
  grace_days_min: 1,
  grace_days_max: 90,
  last_run_at: null as string | null,
  next_run_at: null as string | null,
  last_outcome: null as null | Record<string, unknown>,
  pending: [] as { group_id: string; file_count: number; byte_count: number; run_after: string | null; cancellable: boolean }[],
  current: { count: 0, bytes: 0 } as null | { count: number; bytes: number },
  waiting: { count: 0, bytes: 0, first_eligible_at: null, earliest_deletion_at: null } as null | { count: number; bytes: number; first_eligible_at: string | null; earliest_deletion_at: string | null },
  ready: { count: 0, bytes: 0 } as null | { count: number; bytes: number },
  unseen: { count: 0, bytes: 0 } as null | { count: number; bytes: number },
  totals: null as null | { deleted_count: number; deleted_bytes: number; last_deleted_at: string | null },
  beyond_minimum: false,
  held: null as null | { count: number; bytes: number },
  minimum_storage: null as null | Record<string, unknown>,
};

/** Mirror Cleanup off, with the defaults of migration 0017 (#771). */
export const defaultMirrorCleanup = {
  ...defaultDuplicateCleanup,
  grace_days_max: 180,
  folders: [] as string[],
  retention_lock: null as null | Record<string, unknown>,
  cloud_life: { days_after_disappearance: 14, storage_class: "STANDARD", class_from_day: 0, minimum_days: 0, minimum_until_day: null as number | null, beyond_minimum: false },
};

/** No version in a class with a Minimum Storage Duration (#731). */
export const defaultMinimumStorage = {
  vault_id: 1,
  by_storage_class: [{ storage_class: "STANDARD", minimum_days: 0, within: { count: 0, bytes: 0 }, beyond: { count: 3, bytes: 300 }, remaining_cost: 0, estimated_count: 0 }],
  remaining_cost: 0,
  releases: [] as { month: string; count: number; bytes: number }[],
  estimated_count: 0,
  currency: "EUR",
  pricing_effective_at: "2026-10-01",
  region: "eu-south-1",
  disclaimer: "Estimate from AWS list prices; not an AWS Billing quote.",
  evaluated_at: "2026-11-17T10:00:00+00:00",
};

/** A Vault on a shared bucket with no version yet (#737). */
export const defaultCloudUsage = {
  vault_id: 1,
  bucket: "shared-archive",
  region: "eu-south-1",
  dedicated_bucket: false,
  estimate: {
    versions: 0, bytes: 0, monthly_cost: 0, currency: "EUR", region: "eu-south-1",
    pricing_effective_at: "2026-10-01", by_storage_class: [] as { storage_class: string; versions: number; bytes: number; monthly_cost: number }[],
  },
  measured: { status: "shared_bucket" } as Record<string, unknown>,
};

/** No billing export yet (#738): the state and the setup link. */
export const defaultVaultBilledCost = {
  state: "not_configured",
  bucket: "frostvault-system-k3x9q0ab",
  prefix: "billing/cur/",
  checked_at: "2026-11-17T10:00:00+00:00",
  next_check_at: "2026-11-17T16:00:00+00:00",
  read_at: null as string | null,
  exported_at: null as string | null,
  data_through: null as string | null,
  currency: null as string | null,
  stale: false,
  error_code: null as string | null,
  error_detail: null as string | null,
  vault_id: 1,
  vault_bucket: "frostvault-foto-ab12cd34",
  shared: false,
  periods: [] as { period: string; total: number; categories: Record<string, number> }[],
};

/** The System page's view of the same state (#738). */
export const defaultBillingExport = {
  state: "not_configured",
  bucket: "frostvault-system-k3x9q0ab",
  prefix: "billing/cur/",
  checked_at: "2026-11-17T10:00:00+00:00",
  next_check_at: "2026-11-17T16:00:00+00:00",
  read_at: null as string | null,
  exported_at: null as string | null,
  data_through: null as string | null,
  currency: null as string | null,
  stale: false,
  error_code: null as string | null,
  error_detail: null as string | null,
  periods: [] as unknown[],
};

export const defaultLifecycle = {
  profile: { transitions: [] as { days: number; storage_class: string }[] },
  guided_profiles: {
    standard_only: { transitions: [] as { days: number; storage_class: string }[] },
    economical: {
      transitions: [{ days: 30, storage_class: "STANDARD_IA" }, { days: 90, storage_class: "GLACIER_IR" }],
    },
    deep_archive_early: { transitions: [{ days: 0, storage_class: "DEEP_ARCHIVE" }] },
    recent_backups: { transitions: [{ days: 0, storage_class: "GLACIER_IR" }] },
    archive_tiered: {
      transitions: [
        { days: 30, storage_class: "STANDARD_IA" },
        { days: 90, storage_class: "GLACIER_IR" },
        { days: 365, storage_class: "DEEP_ARCHIVE" },
      ],
    },
  },
};

type HarnessOptions = {
  vaultId?: number;
  vaultName?: string;
  onStopped?: () => void;
  fetchImpl?: ApiFetch;
};

export function createVaultAccessFetch(
  handlers: Record<
    string,
    | Response
    | ((init?: RequestInit) => Response | Promise<Response>)
  > = {},
  { locale = "en" }: { locale?: "en" | "it" } = {},
): Mock<ApiFetch> {
  const messages = loadCatalog(locale);
  return vi.fn<ApiFetch>(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const key = `${method} ${url}`;

    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale, locales: ["en", "it"], messages });
    }

    const byKey = handlers[key];
    if (byKey) {
      return typeof byKey === "function" ? byKey(init) : byKey;
    }
    const byUrl = handlers[url];
    if (byUrl) {
      return typeof byUrl === "function" ? byUrl(init) : byUrl;
    }

    if (/^\/api\/vaults\/\d+\/jobs\?/.test(url)) {
      return jsonResponse({ items: [], groups: [], next_groups_before_id: null });
    }
    if (/^\/api\/vaults\/\d+\/operation-policy$/.test(url)) {
      return jsonResponse(defaultPolicy);
    }
    if (/^\/api\/vaults\/\d+\/lifecycle$/.test(url)) {
      return jsonResponse(defaultLifecycle);
    }
    if (/^\/api\/vaults\/\d+\/duplicate-cleanup$/.test(url) && method === "GET") {
      return jsonResponse(defaultDuplicateCleanup);
    }
    if (/^\/api\/vaults\/\d+\/mirror-cleanup$/.test(url) && method === "GET") {
      return jsonResponse(defaultMirrorCleanup);
    }
    if (/^\/api\/vaults\/\d+\/minimum-storage$/.test(url) && method === "GET") {
      return jsonResponse(defaultMinimumStorage);
    }
    if (/^\/api\/vaults\/\d+\/cloud-usage$/.test(url) && method === "GET") {
      return jsonResponse(defaultCloudUsage);
    }
    if (/^\/api\/vaults\/\d+\/billed-cost$/.test(url) && method === "GET") {
      return jsonResponse(defaultVaultBilledCost);
    }
    if (/^\/api\/vaults\/\d+\/move$/.test(url)) {
      return jsonResponse({ vault_id: 1, targets: [] });
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  });
}

export function renderVaultAccess(
  options: HarnessOptions = {},
): RenderResult & { fetchMock: Mock<ApiFetch> } {
  resetApiClientForTests();
  const fetchMock = options.fetchImpl
    ? (options.fetchImpl as Mock<ApiFetch>)
    : createVaultAccessFetch();
  configureApiClient({ fetch: fetchMock });
  const client = createTestQueryClient();

  const result = render(
    <ApiQueryProvider client={client}>
      <I18nProvider>
        <VaultSettingsPanel
          vaultId={options.vaultId ?? 1}
          vaultName={options.vaultName ?? "Test Archive"}
          onStopped={options.onStopped ?? (() => undefined)}
        />
        <ToastRegion closeLabel="Close notification" />
      </I18nProvider>
    </ApiQueryProvider>,
  );

  return { ...result, fetchMock };
}

export function rerenderVaultAccess(
  ui: ReactElement,
): void {
  // placeholder for typed helpers if needed
  void ui;
}
