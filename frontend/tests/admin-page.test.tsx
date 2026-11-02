import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { AdminPage } from "@/pages/admin/AdminPage";

const messages = JSON.parse(
  readFileSync(
    path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../../app/locales/en.json",
    ),
    "utf8",
  ),
) as Record<string, string>;

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const defaultVaults = [
  {
    id: 1,
    name: "Vault A",
    slug: "vault-a",
    source_root: "/sources/a",
    s3_prefix: "vaults/a/",
    enabled: true,
    encryption_mode: "plain",
    decommission_state: "active",
  },
  {
    id: 2,
    name: "Vault B",
    slug: "vault-b",
    source_root: "/sources/b",
    s3_prefix: "vaults/b/",
    enabled: true,
    encryption_mode: "crypt",
    decommission_state: "active",
  },
];

async function renderAdmin() {
  const requests: Array<{ url: string; method: string }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    requests.push({ url, method });
    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    }
    if (url === "/api/admin/vaults" && method === "GET") {
      return jsonResponse({ items: structuredClone(defaultVaults) });
    }
    if (url === "/api/vaults/2/decommission/preview" && method === "POST") {
      return jsonResponse({
        vault_id: 2,
        vault_name: "Vault B",
        decommission_state: "active",
        enabled: true,
        local_disposition: "retain",
        cloud_disposition: "retain",
        fingerprint: "fp",
        can_start: true,
        blockers: [],
        counts: {
          vault_files: 0,
          local_files: 0,
          local_bytes: 0,
          archive_versions: 0,
          cloud_bytes: 0,
          delete_markers: 0,
          jobs: 0,
        },
        records: {},
      });
    }
    return jsonResponse({ detail: `unexpected ${method} ${url}` }, 500);
  });
  configureApiClient({
    fetch: fetchMock as unknown as typeof fetch,
    navigate: vi.fn(),
    requestPassword: vi.fn(async () => "reauth-password"),
    getAuthMethod: () => "local",
    getPathname: () => "/admin",
    getSearch: () => "",
    csrfToken: "csrf",
  });

  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <AdminPage />
      </I18nProvider>
    </ApiQueryProvider>,
  );
  await screen.findByRole("heading", { name: /vaults and settings/i });
  return { requests };
}

afterEach(() => {
  cleanup();
  resetApiClientForTests();
});

describe("AdminPage — single owner (#412)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("exposes stable routes without Users, Invites or OIDC sections", async () => {
    await renderAdmin();

    const navigation = await screen.findByRole("navigation", {
      name: /administration sections/i,
    });
    const expectedRoutes = [
      ["Overview", "/admin"],
      ["Vaults", "/admin/vaults"],
      ["Source Volumes", "/admin/sources"],
      ["Defaults", "/admin/defaults"],
      ["Notifications", "/admin/notifications"],
      ["Deployment configuration", "/admin/deployment"],
      ["Storage cost estimates", "/admin/storage-cost-estimates"],
      ["Audit events", "/admin/audit-events"],
      ["Worker errors", "/admin/worker-errors"],
      ["Metadata backups", "/admin/metadata-backups"],
    ];
    const links = within(navigation).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(
      expectedRoutes.map(([, href]) => href),
    );
    for (const [name, href] of expectedRoutes) {
      expect(within(navigation).getByRole("link", { name })).toHaveAttribute(
        "href",
        href,
      );
    }
  });

  it("lists every Vault without user, member or vault-creation controls", async () => {
    const { requests } = await renderAdmin();

    expect(await screen.findByText("Vault A")).toBeInTheDocument();
    expect(screen.getByText("Vault B")).toBeInTheDocument();
    expect(screen.queryByText(/new user/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/members?/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /create vault/i })).not.toBeInTheDocument();
    expect(requests.some(({ url }) => url.startsWith("/api/admin/users"))).toBe(false);
  });

  it("previews decommission through the owner Vault endpoint", async () => {
    const user = userEvent.setup();
    const { requests } = await renderAdmin();

    await screen.findByText("Vault B");
    const decommissionButtons = screen.getAllByRole("button", {
      name: /decommission/i,
    });
    await user.click(decommissionButtons[1]!);

    await vi.waitFor(() => {
      expect(
        requests.some(
          ({ url, method }) =>
            url === "/api/vaults/2/decommission/preview" && method === "POST",
        ),
      ).toBe(true);
    });
    expect(requests.some(({ url }) => url.includes("/api/admin/vaults/2/decommission"))).toBe(false);
  });
});
