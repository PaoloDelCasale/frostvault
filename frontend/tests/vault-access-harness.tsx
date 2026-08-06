import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi, type Mock } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
  type ApiFetch,
} from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { VaultAccessPage } from "@/pages/vault-access";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);

export function loadCatalog(locale: "en" | "it" = "en"): Record<string, string> {
  const raw = readFileSync(path.join(localesDir, `${locale}.json`), "utf8");
  return JSON.parse(raw) as Record<string, string>;
}

export function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export const defaultPolicy = {
  auto_upload: true,
  auto_local_cleanup: true,
  local_retention_days: 45,
  stability_seconds: 300,
  include_globs: [] as string[],
  exclude_globs: [] as string[],
  bandwidth_limit_kibps: null as number | null,
  operating_windows: [] as { weekday: number; start: string; end: string }[],
};

export const defaultLifecycle = {
  default_policy_id: null as number | null,
  folder_overrides: [] as { folder_path: string; policy_id: number | string }[],
  policies: [] as { id: number | string; name?: string; profile?: { transitions: { days: number; storage_class: string }[] } }[],
  guided_profiles: {
    standard_only: { transitions: [] as { days: number; storage_class: string }[] },
    ia_after_30: {
      transitions: [{ days: 30, storage_class: "STANDARD_IA" }],
    },
    archive_tiered: {
      transitions: [
        { days: 30, storage_class: "STANDARD_IA" },
        { days: 90, storage_class: "GLACIER" },
      ],
    },
  },
};

export const defaultCloudDeletion = {
  enabled: false,
  purge_delay_seconds: 86400,
  delete_marker_explanation:
    "A Delete Marker is a reversible cloud marker that hides the current key.",
  generated_phrase: "amber-birch-10",
  accepted_single_identity_risk: "Single IAM identity risk documented.",
};

export const defaultQuotas = {
  limits: {},
  usage: {},
  evaluation: { state: "evaluated", allowed: true, decisions: [] as { code?: string; severity?: string }[] },
};

type HarnessOptions = {
  isAdmin?: boolean;
  isVaultOwner?: boolean;
  vaultId?: number;
  vaultName?: string;
  onTransferred?: () => void;
  fetchImpl?: ApiFetch;
};

export function createVaultAccessFetch(
  handlers: Record<
    string,
    | Response
    | ((init?: RequestInit) => Response | Promise<Response>)
  > = {},
): Mock<ApiFetch> {
  const en = loadCatalog("en");
  return vi.fn<ApiFetch>(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const key = `${method} ${url}`;

    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages: en });
    }

    const byKey = handlers[key];
    if (byKey) {
      return typeof byKey === "function" ? byKey(init) : byKey;
    }
    const byUrl = handlers[url];
    if (byUrl) {
      return typeof byUrl === "function" ? byUrl(init) : byUrl;
    }

    if (url === "/api/vault/members" && method === "GET") {
      return jsonResponse({ items: [] });
    }
    if (url === "/api/vault/quotas") {
      return jsonResponse(defaultQuotas);
    }
    if (url === "/api/vault/operation-policy") {
      return jsonResponse(defaultPolicy);
    }
    if (url === "/api/vault/lifecycle") {
      return jsonResponse(defaultLifecycle);
    }
    if (url === "/api/vault/cloud-deletion") {
      return jsonResponse(defaultCloudDeletion);
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
  const client = createAppQueryClient();

  const result = render(
    <ApiQueryProvider client={client}>
      <I18nProvider>
        <VaultAccessPage
          vaultId={options.vaultId ?? 1}
          vaultName={options.vaultName ?? "Test Archive"}
          isAdmin={options.isAdmin ?? true}
          isVaultOwner={options.isVaultOwner ?? false}
          onTransferred={options.onTransferred}
        />
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
