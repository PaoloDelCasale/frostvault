import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { AdminPage } from "@/pages/admin/AdminPage";
import { StorageCostEstimatesSection } from "@/pages/admin/StorageCostEstimatesSection";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);
const messages = JSON.parse(
  readFileSync(path.join(localesDir, "en.json"), "utf8"),
) as Record<string, string>;

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function estimate(storageClass: string, sizeBytes: number) {
  const rates: Record<string, number> = {
    STANDARD: 0.046,
    GLACIER: 0.008,
    DEEP_ARCHIVE: 0.00198,
  };
  return {
    kind: "storage_month",
    size_bytes: sizeBytes,
    storage_class: storageClass,
    tier: null,
    estimated_cost_eur: rates[storageClass] ?? 0,
    estimated_hours: null,
    currency: "EUR",
    price_book_id: 7,
    price_book_name: "July 2026 rates",
    pricing_effective_at: "2026-07-01T00:00:00+00:00",
    assumptions: { region: "eu-south-1" },
  };
}

function renderSection(fetchMock: ReturnType<typeof vi.fn>) {
  configureApiClient({ fetch: fetchMock, csrfToken: "csrf" });
  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <StorageCostEstimatesSection />
      </I18nProvider>
    </ApiQueryProvider>,
  );
}

const vaults = [
  {
    id: 1,
    name: "Photos",
    slug: "photos",
    source_root: "/sources/photos",
    s3_prefix: "vaults/photos/",
    enabled: true,
    member_count: 1,
  },
  {
    id: 2,
    name: "Documents",
    slug: "documents",
    source_root: "/sources/documents",
    s3_prefix: "vaults/documents/",
    enabled: true,
    member_count: 1,
  },
];

afterEach(() => {
  resetApiClientForTests();
  window.history.replaceState({}, "", "/");
});

it("prices the selected Vault aggregate as three clearly labelled alternatives", async () => {
  const user = userEvent.setup();
  const estimateBodies: Array<{ size_bytes: number; storage_class: string }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    }
    if (url === "/api/admin/vaults") return jsonResponse({ items: vaults });
    if (url === "/api/admin/vaults/1/quotas") {
      return jsonResponse({
        vault_id: 1,
        limits: {},
        usage: { storage_bytes: 2 * 1024 ** 3, storage_unknown: false },
        evaluation: {},
      });
    }
    if (url === "/api/admin/vaults/2/quotas") {
      return jsonResponse({
        vault_id: 2,
        limits: {},
        usage: { storage_bytes: 0, storage_unknown: false },
        evaluation: {},
      });
    }
    if (url === "/api/admin/cost-estimates/storage") {
      const body = JSON.parse(String(init?.body)) as {
        size_bytes: number;
        storage_class: string;
      };
      estimateBodies.push(body);
      return jsonResponse(estimate(body.storage_class, body.size_bytes));
    }
    throw new Error(`Unexpected request: ${url}`);
  });

  renderSection(fetchMock);

  expect(await screen.findByTestId("aggregate-vault-size")).toHaveTextContent("2.0 GB");
  expect(screen.getByRole("heading", { name: "Standard scenario" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Glacier scenario" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Deep Archive scenario" })).toBeInTheDocument();
  expect(screen.getAllByText("July 2026 rates")).toHaveLength(3);
  expect(screen.getAllByText("2026-07-01T00:00:00+00:00")).toHaveLength(3);
  expect(estimateBodies).toEqual([
    { size_bytes: 2 * 1024 ** 3, storage_class: "STANDARD" },
    { size_bytes: 2 * 1024 ** 3, storage_class: "GLACIER" },
    { size_bytes: 2 * 1024 ** 3, storage_class: "DEEP_ARCHIVE" },
  ]);

  await user.selectOptions(screen.getByLabelText("Vault"), "2");
  expect(await screen.findByText(/all three estimates are zero/i)).toBeInTheDocument();
  await waitFor(() => expect(estimateBodies).toHaveLength(6));
  expect(estimateBodies.slice(3)).toEqual([
    { size_bytes: 0, storage_class: "STANDARD" },
    { size_bytes: 0, storage_class: "GLACIER" },
    { size_bytes: 0, storage_class: "DEEP_ARCHIVE" },
  ]);
});

it("shows loading and empty Vault states without requesting estimates", async () => {
  let releaseVaults!: (response: Response) => void;
  const pendingVaults = new Promise<Response>((resolve) => {
    releaseVaults = resolve;
  });
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    }
    if (url === "/api/admin/vaults") return pendingVaults;
    throw new Error(`Unexpected request: ${url}`);
  });

  renderSection(fetchMock);
  expect(await screen.findByText("Loading Vaults…")).toBeInTheDocument();
  releaseVaults(jsonResponse({ items: [] }));

  expect(await screen.findByText("No Vaults are available for estimation.")).toBeInTheDocument();
  expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("cost-estimates"))).toBe(false);
});

it("does not estimate a Vault whose aggregate stored size is unknown", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    }
    if (url === "/api/admin/vaults") return jsonResponse({ items: [vaults[0]] });
    if (url === "/api/admin/vaults/1/quotas") {
      return jsonResponse({
        vault_id: 1,
        limits: {},
        usage: { storage_bytes: 123, storage_unknown: true },
        evaluation: {},
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  });

  renderSection(fetchMock);

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "includes Archive Versions with an unknown size",
  );
  expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("cost-estimates"))).toBe(false);
  expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
});

it("routes the pathname-only admin navigation to storage estimates", async () => {
  window.history.replaceState({}, "", "/admin/storage-cost-estimates");
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    }
    if (url === "/api/me") {
      return jsonResponse({
        id: 1,
        username: "admin",
        display_name: "Admin",
        is_admin: true,
        active: true,
        session_version: 1,
        csrf_token: "csrf",
        offline_cache_generation: "admin-cost-session-vault-1",
        auth_method: "local",
        locale: "en",
        locales: ["en", "it"],
        vault: null,
      });
    }
    if (url === "/api/admin/vaults") return jsonResponse({ items: [vaults[0]] });
    if (url === "/api/admin/vaults/1/quotas") {
      return jsonResponse({ limits: {}, usage: { storage_bytes: 1024 }, evaluation: {} });
    }
    if (url === "/api/admin/cost-estimates/storage") {
      const body = JSON.parse(String(init?.body)) as {
        size_bytes: number;
        storage_class: string;
      };
      return jsonResponse(estimate(body.storage_class, body.size_bytes));
    }
    throw new Error(`Unexpected request: ${url}`);
  });
  configureApiClient({ fetch: fetchMock, csrfToken: "csrf" });

  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <AdminPage />
      </I18nProvider>
    </ApiQueryProvider>,
  );

  expect(
    await screen.findByRole("heading", { name: "Storage cost estimates", level: 2 }),
  ).toBeInTheDocument();
  const navigation = screen.getByRole("navigation", {
    name: "Administration sections",
  });
  expect(within(navigation).getByRole("link", { name: "Storage cost estimates" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  expect(fetchMock.mock.calls.some((call) => String(call[0]) === "/api/admin/users")).toBe(false);
});
