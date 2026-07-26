import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { AdminPage } from "@/pages/admin/AdminPage";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);

function loadMessages(): Record<string, string> {
  return JSON.parse(
    readFileSync(path.join(localesDir, "en.json"), "utf8"),
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

const messages = loadMessages();

const defaultUsers = [
  {
    id: 10,
    display_name: "A user",
    username: "a-user",
    active: true,
    is_admin: true,
    vault_count: 1,
  },
  {
    id: 20,
    display_name: "B user",
    username: "b-user",
    active: true,
    is_admin: false,
    vault_count: 0,
  },
];

const defaultVaults = [
  {
    id: 1,
    name: "Vault A",
    slug: "vault-a",
    source_root: "/sources/a",
    s3_prefix: "vaults/a/",
    enabled: true,
    member_count: 1,
    encryption_mode: "plain",
  },
  {
    id: 2,
    name: "Vault B",
    slug: "vault-b",
    source_root: "/sources/b",
    s3_prefix: "vaults/b/",
    enabled: true,
    member_count: 1,
    encryption_mode: "crypt",
  },
];

type HarnessOptions = {
  isAdmin?: boolean;
  authMethod?: string | null;
  users?: typeof defaultUsers;
  vaults?: typeof defaultVaults;
};

async function renderAdmin(options: HarnessOptions = {}) {
  const {
    isAdmin = true,
    authMethod = "local",
    users = structuredClone(defaultUsers),
    vaults = structuredClone(defaultVaults),
  } = options;

  const navigate = vi.fn();
  const requestPassword = vi.fn(async () => "reauth-password");
  const memberGets = new Map<number, ReturnType<typeof deferred<Response>>>();
  const quotaGets: Array<{
    vaultId: number;
    method: string;
    body?: string;
    deferred: ReturnType<typeof deferred<Response>>;
  }> = [];
  const transferPosts: Array<{
    url: string;
    body: string;
    deferred: ReturnType<typeof deferred<Response>>;
  }> = [];
  const mutatingCalls: Array<{ url: string; method: string; body: string }> =
    [];

  let usersState = users;
  let vaultsState = vaults;

  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      const body = typeof init?.body === "string" ? init.body : "";

      if (url.startsWith("/api/i18n/catalog")) {
        return jsonResponse({
          locale: "en",
          locales: ["en", "it"],
          messages,
        });
      }
      if (url === "/api/me") {
        return jsonResponse({
          id: 1,
          username: "admin",
          display_name: "Admin",
          is_admin: isAdmin,
          active: true,
          session_version: 1,
          csrf_token: "csrf",
          auth_method: authMethod,
          locale: "en",
          locales: ["en", "it"],
          vault: null,
        });
      }
      if (url === "/api/reauth" && method === "POST") {
        return jsonResponse({ ok: true });
      }
      if (url === "/api/admin/users" && method === "GET") {
        return jsonResponse({ items: usersState });
      }
      if (url === "/api/admin/users" && method === "POST") {
        mutatingCalls.push({ url, method, body });
        const payload = JSON.parse(body) as {
          display_name: string;
          username: string;
          password: string;
          is_admin: boolean;
        };
        const created = {
          id: 99,
          display_name: payload.display_name,
          username: payload.username,
          is_admin: payload.is_admin,
          active: true,
          vault_count: 0,
        };
        usersState = [...usersState, created];
        return jsonResponse(created, 201);
      }
      if (url.match(/^\/api\/admin\/users\/\d+$/) && method === "PATCH") {
        mutatingCalls.push({ url, method, body });
        const id = Number(url.split("/").pop());
        const payload = JSON.parse(body) as {
          active?: boolean;
          password?: string;
        };
        if (
          payload.active === false &&
          usersState.find((u) => u.id === id)?.is_admin &&
          usersState.filter((u) => u.is_admin && u.active).length <= 1
        ) {
          return jsonResponse(
            { detail: "At least one administrator must remain active" },
            400,
          );
        }
        usersState = usersState.map((u) =>
          u.id === id
            ? {
                ...u,
                active: payload.active ?? u.active,
              }
            : u,
        );
        const updated = usersState.find((u) => u.id === id)!;
        return jsonResponse(updated);
      }
      if (url === "/api/admin/vaults" && method === "GET") {
        return jsonResponse({ items: vaultsState });
      }
      if (url === "/api/admin/vaults" && method === "POST") {
        mutatingCalls.push({ url, method, body });
        const payload = JSON.parse(body) as {
          name: string;
          slug: string;
          owner_user_id: number;
          reason: string;
          encryption_mode?: string;
          source_root?: string;
        };
        const created = {
          id: 50,
          name: payload.name,
          slug: payload.slug,
          source_root: `/sources/${payload.slug}`,
          s3_prefix: `vaults/${payload.slug}/`,
          enabled: true,
          member_count: 1,
          encryption_mode: payload.encryption_mode ?? "plain",
        };
        vaultsState = [...vaultsState, created];
        return jsonResponse(created, 201);
      }

      const quotaMatch = url.match(/^\/api\/admin\/vaults\/(\d+)\/quotas$/);
      if (quotaMatch) {
        const vaultId = Number(quotaMatch[1]);
        const entry = {
          vaultId,
          method,
          body,
          deferred: deferred<Response>(),
        };
        quotaGets.push(entry);
        if (method === "PUT") mutatingCalls.push({ url, method, body });
        return entry.deferred.promise;
      }

      const membersMatch = url.match(
        /^\/api\/admin\/vaults\/(\d+)\/members$/,
      );
      if (membersMatch && method === "GET") {
        const vaultId = Number(membersMatch[1]);
        const entry = deferred<Response>();
        memberGets.set(vaultId, entry);
        return entry.promise;
      }
      if (membersMatch && method === "POST") {
        mutatingCalls.push({ url, method, body });
        return jsonResponse({ ok: true }, 201);
      }

      const transferMatch = url.match(
        /^\/api\/admin\/vaults\/(\d+)\/transfer-owner$/,
      );
      if (transferMatch && method === "POST") {
        const entry = {
          url,
          body,
          deferred: deferred<Response>(),
        };
        transferPosts.push(entry);
        mutatingCalls.push({ url, method, body });
        return entry.deferred.promise;
      }

      if (
        url.match(/^\/api\/admin\/vaults\/\d+\/recovery\/export$/) &&
        method === "POST"
      ) {
        mutatingCalls.push({ url, method, body });
        return jsonResponse({ recovery_export: "RECOVERY-SECRET-MATERIAL" });
      }

      if (
        url.match(/^\/api\/admin\/vaults\/\d+\/members\/\d+/) &&
        method === "DELETE"
      ) {
        mutatingCalls.push({ url, method, body });
        return jsonResponse({ ok: true });
      }

      throw new Error(`Unexpected request: ${method} ${url}`);
    },
  );

  configureApiClient({
    fetch: fetchMock,
    navigate,
    requestPassword,
    getAuthMethod: () => authMethod,
    getPathname: () => "/admin",
    getSearch: () => "",
    csrfToken: "csrf",
  });

  const client = createAppQueryClient();
  render(
    <ApiQueryProvider client={client}>
      <I18nProvider>
        <AdminPage />
      </I18nProvider>
    </ApiQueryProvider>,
  );

  if (isAdmin) {
    await screen.findByRole("heading", { name: /users and vaults/i });
  }

  return {
    fetchMock,
    navigate,
    requestPassword,
    mutatingCalls,
    memberGets,
    quotaGets,
    transferPosts,
    getUsers: () => usersState,
    getVaults: () => vaultsState,
  };
}

function unlimitedQuotaResponse(vaultId = 1) {
  return jsonResponse({
    vault_id: vaultId,
    limits: {
      storage_soft_limit_bytes: null,
      storage_hard_limit_bytes: null,
      concurrency_soft_limit: null,
      concurrency_hard_limit: null,
      restore_30d_soft_limit_bytes: null,
      restore_30d_hard_limit_bytes: null,
    },
    usage: { storage_bytes: 0, concurrency: 0, restore_30d_bytes: 0 },
    evaluation: { state: "evaluated", allowed: true, decisions: [] },
  });
}

const membersA = {
  items: [
    {
      id: 10,
      display_name: "A member",
      username: "a-member",
      active: true,
      role: "operator",
    },
  ],
};

const membersB = {
  items: [
    {
      id: 20,
      display_name: "B member",
      username: "b-member",
      active: true,
      role: "operator",
    },
  ],
};

const membersWithInactive = {
  items: [
    {
      id: 10,
      display_name: "Current owner",
      username: "owner",
      active: true,
      role: "owner",
    },
    {
      id: 20,
      display_name: "Inactive target",
      username: "inactive-target",
      active: false,
      role: "operator",
    },
    {
      id: 30,
      display_name: "Active target",
      username: "active-target",
      active: true,
      role: "viewer",
    },
  ],
};

afterEach(() => {
  cleanup();
  resetApiClientForTests();
});

describe("AdminPage — create user (seam 1)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("creates a user with the right payload and refreshes the list", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin();

    await user.type(
      screen.getByLabelText(/display name/i),
      "New Person",
    );
    await user.type(screen.getByLabelText(/^username$/i), "new-person");
    await user.type(
      screen.getByLabelText(/initial password/i),
      "long-enough-password",
    );
    await user.click(screen.getByRole("button", { name: /create user/i }));

    await waitFor(() => {
      const create = harness.mutatingCalls.find(
        (c) => c.url === "/api/admin/users" && c.method === "POST",
      );
      expect(create).toBeTruthy();
      expect(JSON.parse(create!.body)).toEqual({
        display_name: "New Person",
        username: "new-person",
        password: "long-enough-password",
        is_admin: false,
      });
    });

    await waitFor(() => {
      expect(screen.getByText("New Person")).toBeInTheDocument();
    });
  });
});
