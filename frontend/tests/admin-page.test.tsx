import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  cleanup,
  fireEvent,
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
    has_password: true,
    identity_count: 1,
  },
  {
    id: 20,
    display_name: "B user",
    username: "b-user",
    active: true,
    is_admin: false,
    vault_count: 0,
    has_password: false,
    identity_count: 0,
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
  /** When set, the first N quota PUTs return reauth_required before succeeding/failing. */
  quotaPutReauthTimes?: number;
  quotaPutFinal?: { status: number; body: unknown };
};

async function renderAdmin(options: HarnessOptions = {}) {
  const {
    isAdmin = true,
    authMethod = "local",
    users = structuredClone(defaultUsers),
    vaults = structuredClone(defaultVaults),
    quotaPutReauthTimes = 0,
    quotaPutFinal,
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
  let remainingQuotaReauths = quotaPutReauthTimes;

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
          password: string | null;
          is_admin: boolean;
        };
        const created = {
          id: 99,
          display_name: payload.display_name,
          username: payload.username,
          is_admin: payload.is_admin,
          active: true,
          vault_count: 0,
          has_password: payload.password !== null,
          identity_count: 0,
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
          is_admin?: boolean;
          display_name?: string;
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
                is_admin: payload.is_admin ?? u.is_admin,
                display_name: payload.display_name ?? u.display_name,
              }
            : u,
        );
        const updated = usersState.find((u) => u.id === id)!;
        return jsonResponse(updated);
      }
      if (url === "/api/admin/vaults" && method === "GET") {
        return jsonResponse({ items: vaultsState });
      }
      if (url === "/api/admin/source-volumes" && method === "GET") {
        return jsonResponse({ items: [] });
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
        if (method === "PUT") {
          mutatingCalls.push({ url, method, body });
          if (remainingQuotaReauths > 0) {
            remainingQuotaReauths -= 1;
            return jsonResponse({ error: "reauth_required" }, 403);
          }
          if (quotaPutFinal) {
            return jsonResponse(quotaPutFinal.body, quotaPutFinal.status);
          }
        }
        const entry = {
          vaultId,
          method,
          body,
          deferred: deferred<Response>(),
        };
        quotaGets.push(entry);
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

describe("AdminPage — administration navigation (issue #136 seam 1)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("exposes stable routes for every administration section", async () => {
    await renderAdmin();

    const navigation = await screen.findByRole("navigation", {
      name: /administration sections/i,
    });
    const expectedRoutes = [
      ["Overview", "/admin"],
      ["Users and identities", "/admin/users"],
      ["Vaults", "/admin/vaults"],
      ["Source Volumes", "/admin/sources"],
      ["Defaults", "/admin/defaults"],
      ["OIDC", "/admin/oidc"],
      ["Notifications", "/admin/notifications"],
      ["Deployment configuration", "/admin/deployment"],
      ["Cost price books", "/admin/cost-price-books"],
      ["Storage cost estimates", "/admin/storage-cost-estimates"],
      ["Audit events", "/admin/audit-events"],
      ["Worker errors", "/admin/worker-errors"],
      ["Metadata backups", "/admin/metadata-backups"],
    ];

    for (const [name, href] of expectedRoutes) {
      expect(within(navigation).getByRole("link", { name })).toHaveAttribute(
        "href",
        href,
      );
    }
  });
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

describe("AdminPage — passwordless user creation (issue #136 seam 2)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("creates a passwordless User explicitly and shows authentication status", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin();

    expect(screen.getByText(/1 linked identity/i)).toBeInTheDocument();
    expect(screen.getByText(/no sign-in method configured/i)).toBeInTheDocument();

    await user.type(screen.getByLabelText(/display name/i), "OIDC Person");
    await user.type(screen.getByLabelText(/^username$/i), "oidc-person");
    await user.click(screen.getByLabelText(/create without a password/i));
    await user.click(screen.getByRole("button", { name: /create user/i }));

    await waitFor(() => {
      const create = harness.mutatingCalls.find(
        (call) => call.url === "/api/admin/users" && call.method === "POST",
      );
      expect(JSON.parse(create!.body)).toEqual({
        display_name: "OIDC Person",
        username: "oidc-person",
        password: null,
        is_admin: false,
      });
    });
  });
});

describe("AdminPage — create vault (seam 2)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("creates a vault with the right payload including server-minted source root on refresh", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin();

    await user.type(screen.getByLabelText(/vault name/i), "Ops Vault");
    await user.type(screen.getByLabelText(/vault code/i), "ops-vault");
    await user.type(
      screen.getByLabelText(/reason for administrator creation/i),
      "provision for ops",
    );
    await user.click(
      screen.getByRole("button", { name: /create private vault/i }),
    );

    await waitFor(() => {
      const create = harness.mutatingCalls.find(
        (c) => c.url === "/api/admin/vaults" && c.method === "POST",
      );
      expect(create).toBeTruthy();
      const body = JSON.parse(create!.body) as Record<string, unknown>;
      expect(body).toEqual({
        name: "Ops Vault",
        slug: "ops-vault",
        owner_user_id: 10,
        reason: "provision for ops",
        encryption_mode: "plain",
        creation_mode: "empty",
      });
      // Source root is minted by the server — never sent by the client.
      expect(body).not.toHaveProperty("source_root");
      expect(body).not.toHaveProperty("volume_alias");
      expect(body).not.toHaveProperty("relative_path");
    });

    await waitFor(() => {
      expect(screen.getByText("Ops Vault")).toBeInTheDocument();
      expect(screen.getByText(/\/sources\/ops-vault/)).toBeInTheDocument();
    });
  });
});

describe("AdminPage — enable/disable user (seam 3)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("PATCHes active correctly and surfaces last-admin refusal (BUG-020)", async () => {
    const user = userEvent.setup();
    await renderAdmin();

    const deactivateButtons = await screen.findAllByRole("button", {
      name: /deactivate/i,
    });
    // First user in default fixture is the sole admin (id 10).
    await user.click(deactivateButtons[0]!);
    let confirmation = await screen.findByRole("alertdialog");
    await user.click(within(confirmation).getByRole("button", { name: /confirm deactivation/i }));

    await waitFor(() => {
      expect(
        screen.getByRole("alert"),
      ).toHaveTextContent(/at least one administrator must remain active/i);
    });

    // Non-admin user can be deactivated.
    const remaining = screen.getAllByRole("button", { name: /deactivate/i });
    await user.click(remaining[remaining.length - 1]!);
    confirmation = await screen.findByRole("alertdialog");
    await user.click(within(confirmation).getByRole("button", { name: /confirm deactivation/i }));

    await waitFor(() => {
      expect(screen.getByText(/user deactivated/i)).toBeInTheDocument();
    });
  });
});

describe("AdminPage — User profile editing (issue #136 seam 2)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("updates the display name through an accessible dialog", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin();

    await user.click(await screen.findByRole("button", { name: /edit b user/i }));
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByLabelText(/display name/i);
    await user.clear(input);
    await user.type(input, "Bee User");
    await user.click(within(dialog).getByRole("button", { name: /save display name/i }));

    await waitFor(() => {
      const patch = harness.mutatingCalls.find(
        (call) => call.url === "/api/admin/users/20" && call.body.includes("display_name"),
      );
      expect(JSON.parse(patch!.body)).toEqual({ display_name: "Bee User" });
      expect(screen.getByText("Bee User")).toBeInTheDocument();
    });
  });
});

describe("AdminPage — global role changes (issue #136 seam 2)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("requires confirmation before promoting a User", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin();

    const promote = await screen.findByRole("button", { name: /promote b user/i });
    await user.click(promote);
    const confirmation = await screen.findByRole("alertdialog");
    await user.click(within(confirmation).getByRole("button", { name: /confirm promotion/i }));

    await waitFor(() => {
      const patch = harness.mutatingCalls.find(
        (call) => call.url === "/api/admin/users/20" && call.body.includes("is_admin"),
      );
      expect(JSON.parse(patch!.body)).toEqual({ is_admin: true });
    });
  });
});

describe("AdminPage — password reset dialog (seam 4)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("resets password in a dialog, never window.prompt, and clears the value", async () => {
    const user = userEvent.setup();
    const promptSpy = vi.spyOn(window, "prompt");
    const harness = await renderAdmin();

    const resetButtons = await screen.findAllByRole("button", {
      name: /new password/i,
    });
    await user.click(resetButtons[0]!);

    const dialog = await screen.findByRole("dialog");
    const passwordInput = within(dialog).getByLabelText(/^password$/i);
    await user.type(passwordInput, "brand-new-password");
    expect(passwordInput).toHaveValue("brand-new-password");

    await user.click(
      within(dialog).getByRole("button", { name: /update password/i }),
    );

    await waitFor(() => {
      const patch = harness.mutatingCalls.find(
        (c) =>
          c.url === "/api/admin/users/10" &&
          c.method === "PATCH" &&
          c.body.includes("password"),
      );
      expect(patch).toBeTruthy();
      expect(JSON.parse(patch!.body)).toEqual({
        password: "brand-new-password",
      });
    });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(promptSpy).not.toHaveBeenCalled();
    // Toast must not echo the password.
    expect(screen.getByText(/password updated/i)).toBeInTheDocument();
    expect(screen.queryByText("brand-new-password")).not.toBeInTheDocument();
    promptSpy.mockRestore();
  });
});

async function openVaultMembers(
  user: ReturnType<typeof userEvent.setup>,
  vaultName: string,
) {
  const manageButtons = await screen.findAllByRole("button", {
    name: /manage access/i,
  });
  // Vault list order matches fixture: Vault A then Vault B.
  const index = vaultName === "Vault B" ? 1 : 0;
  await user.click(manageButtons[index]!);
  await screen.findByRole("dialog");
}

describe("AdminPage — members dialog load (seam 5)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("loads membership and quotas for the selected vault without leaking prior state", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin();

    await openVaultMembers(user, "Vault A");
    harness.memberGets.get(1)!.resolve(jsonResponse(membersA));
    harness.quotaGets
      .find((q) => q.vaultId === 1 && q.method === "GET")!
      .deferred.resolve(unlimitedQuotaResponse(1));

    await waitFor(() => {
      expect(screen.getByText("A member")).toBeInTheDocument();
    });

    // Close and open Vault B.
    await user.click(screen.getByRole("button", { name: /close/i }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    await openVaultMembers(user, "Vault B");
    // Members list must be cleared while loading (no A member leak).
    expect(screen.queryByText("A member")).not.toBeInTheDocument();

    harness.memberGets.get(2)!.resolve(jsonResponse(membersB));
    harness.quotaGets
      .find((q) => q.vaultId === 2 && q.method === "GET")!
      .deferred.resolve(
        jsonResponse({
          vault_id: 2,
          limits: { storage_soft_limit_bytes: 22 },
          usage: { storage_bytes: 2 },
          evaluation: { state: "evaluated", allowed: true, decisions: [] },
        }),
      );

    await waitFor(() => {
      expect(screen.getByText("B member")).toBeInTheDocument();
      expect(screen.queryByText("A member")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      const soft = screen.getByLabelText(/storage soft limit/i);
      expect(soft).toHaveValue(22);
    });
  });
});

describe("AdminPage — recovery export clipboard feedback (#203)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("reports rejected copies without implying success or hiding fallback", async () => {
    const user = userEvent.setup();
    const recoveryExport = "RECOVERY-SECRET-MATERIAL";
    const writeText = vi
      .fn()
      .mockRejectedValueOnce(new Error("clipboard permission denied"))
      .mockResolvedValueOnce(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const harness = await renderAdmin();

    await openVaultMembers(user, "Vault B");
    harness.memberGets.get(2)!.resolve(jsonResponse(membersB));
    harness.quotaGets
      .find((q) => q.vaultId === 2 && q.method === "GET")!
      .deferred.resolve(unlimitedQuotaResponse(2));

    await user.type(
      screen.getByLabelText(messages["admin.recovery_reason"]),
      "save the export offline",
    );
    await user.click(
      screen.getByRole("button", {
        name: messages["admin.recovery_export_submit"],
      }),
    );
    await screen.findByText(recoveryExport);

    const copyButton = screen.getByRole("button", {
      name: messages["admin.recovery_copy"],
    });
    const downloadButton = screen.getByRole("button", {
      name: messages["admin.recovery_download"],
    });
    await user.click(copyButton);

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent(messages["admin.recovery_copy_failed"]);
    expect(failure).not.toHaveTextContent(recoveryExport);
    expect(writeText).toHaveBeenCalledWith(recoveryExport);
    expect(screen.queryByText(messages["admin.recovery_copied"])).not.toBeInTheDocument();
    expect(screen.getByText(recoveryExport)).toBeInTheDocument();
    expect(downloadButton).toBeEnabled();

    await user.click(copyButton);
    expect(
      await screen.findByText(messages["admin.recovery_copied"]),
    ).toHaveAttribute("role", "status");
  });
});

describe("AdminPage — quota save (seam 6)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("saves blank limits as null and rejects invalid soft>hard without sending", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin();

    await openVaultMembers(user, "Vault A");
    harness.memberGets.get(1)!.resolve(jsonResponse(membersA));
    harness.quotaGets
      .find((q) => q.vaultId === 1 && q.method === "GET")!
      .deferred.resolve(unlimitedQuotaResponse(1));

    await waitFor(() => {
      expect(screen.getByLabelText(/storage soft limit/i)).toBeEnabled();
    });

    await user.type(
      screen.getByLabelText(/reason for this quota change/i),
      "remove quota limits",
    );
    await user.click(screen.getByRole("button", { name: /save quotas/i }));

    await waitFor(() => {
      const put = harness.quotaGets.find(
        (q) => q.vaultId === 1 && q.method === "PUT",
      );
      expect(put).toBeTruthy();
    });
    const put = harness.quotaGets.find(
      (q) => q.vaultId === 1 && q.method === "PUT",
    )!;
    expect(JSON.parse(put.body!)).toEqual({
      storage_soft_limit_bytes: null,
      storage_hard_limit_bytes: null,
      concurrency_soft_limit: null,
      concurrency_hard_limit: null,
      restore_30d_soft_limit_bytes: null,
      restore_30d_hard_limit_bytes: null,
      reason: "remove quota limits",
    });
    put.deferred.resolve(unlimitedQuotaResponse(1));

    await waitFor(() => {
      expect(screen.getByText(/vault quotas updated/i)).toBeInTheDocument();
    });

    // Invalid order: soft > hard — no additional PUT.
    const putsBefore = harness.mutatingCalls.filter(
      (c) => c.method === "PUT" && c.url.includes("/quotas"),
    ).length;
    fireEvent.change(screen.getByLabelText(/storage soft limit/i), {
      target: { value: "9" },
    });
    fireEvent.change(screen.getByLabelText(/storage hard limit/i), {
      target: { value: "4" },
    });
    fireEvent.change(screen.getByLabelText(/reason for this quota change/i), {
      target: { value: "bad order" },
    });
    await user.click(screen.getByRole("button", { name: /save quotas/i }));

    await waitFor(() => {
      expect(screen.getByText(/cannot exceed/i)).toBeInTheDocument();
    });
    expect(
      harness.mutatingCalls.filter(
        (c) => c.method === "PUT" && c.url.includes("/quotas"),
      ).length,
    ).toBe(putsBefore);
  });

  it("formats backend quota decisions and unavailable evaluation", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin();

    await openVaultMembers(user, "Vault A");
    harness.memberGets.get(1)!.resolve(jsonResponse(membersA));
    harness.quotaGets
      .find((q) => q.vaultId === 1 && q.method === "GET")!
      .deferred.resolve(
        jsonResponse({
          limits: {},
          usage: {},
          evaluation: {
            state: "evaluated",
            allowed: true,
            decisions: [
              {
                code: "quota.storage.soft_exceeded",
                severity: "warning",
                projected: 11,
                limit: 10,
              },
            ],
          },
        }),
      );

    await waitFor(() => {
      expect(
        screen.getByText(/Warning: quota\.storage\.soft_exceeded/),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByText(/No active warnings or blocks reported/),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /close/i }));
    await openVaultMembers(user, "Vault B");
    harness.memberGets.get(2)!.resolve(jsonResponse(membersB));
    harness.quotaGets
      .find((q) => q.vaultId === 2 && q.method === "GET")!
      .deferred.resolve(jsonResponse({ limits: {}, usage: {} }));

    await waitFor(() => {
      expect(screen.getByText(/Quota state unavailable/)).toBeInTheDocument();
    });
  });
});

describe("AdminPage — ownership transfer (seam 7)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("requires typed confirmation, posts the correct endpoint, and refreshes", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin();

    await openVaultMembers(user, "Vault B");
    harness.memberGets.get(2)!.resolve(jsonResponse(membersB));
    harness.quotaGets
      .find((q) => q.vaultId === 2 && q.method === "GET")!
      .deferred.resolve(unlimitedQuotaResponse(2));

    await waitFor(() => {
      expect(screen.getByLabelText(/new primary owner/i)).toBeEnabled();
    });

    await user.type(
      screen.getByLabelText(/reason for ownership transfer/i),
      "active owner transfer",
    );
    // Wrong confirmation blocks.
    await user.type(
      screen.getByLabelText(/type the vault name to confirm/i),
      "wrong",
    );
    await user.click(
      screen.getByRole("button", { name: /transfer ownership/i }),
    );
    await waitFor(() => {
      expect(screen.getByText(/exact vault name/i)).toBeInTheDocument();
    });
    expect(harness.transferPosts.length).toBe(0);

    await user.clear(
      screen.getByLabelText(/type the vault name to confirm/i),
    );
    await user.type(
      screen.getByLabelText(/type the vault name to confirm/i),
      "Vault B",
    );
    await user.click(
      screen.getByRole("button", { name: /transfer ownership/i }),
    );

    await waitFor(() => {
      expect(harness.transferPosts.length).toBe(1);
    });
    expect(harness.transferPosts[0]!.url).toBe(
      "/api/admin/vaults/2/transfer-owner",
    );
    expect(JSON.parse(harness.transferPosts[0]!.body)).toEqual({
      new_owner_user_id: 20,
      reason: "active owner transfer",
    });
    harness.transferPosts[0]!.deferred.resolve(jsonResponse({}));
    // Refresh members after success.
    await waitFor(() => {
      expect(harness.memberGets.size).toBeGreaterThanOrEqual(1);
    });
    const refresh = [...harness.memberGets.values()].at(-1)!;
    refresh.resolve(jsonResponse(membersB));

    await waitFor(() => {
      expect(screen.getByText(/ownership transferred/i)).toBeInTheDocument();
    });
  });

  it("excludes inactive members from ownership transfer targets", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin();

    await openVaultMembers(user, "Vault A");
    harness.memberGets.get(1)!.resolve(jsonResponse(membersWithInactive));
    harness.quotaGets
      .find((q) => q.vaultId === 1 && q.method === "GET")!
      .deferred.resolve(unlimitedQuotaResponse(1));

    await waitFor(() => {
      const select = screen.getByLabelText(/new primary owner/i);
      expect(select).toBeEnabled();
      expect(select).toHaveTextContent(/Active target/);
      expect(select).not.toHaveTextContent(/Inactive target/);
    });
  });
});

describe("AdminPage — stale requests (seam 8)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("never renders an older vault response over a newer selection", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin();

    await openVaultMembers(user, "Vault A");
    const requestA = harness.memberGets.get(1)!;

    await user.click(screen.getByRole("button", { name: /close/i }));
    await openVaultMembers(user, "Vault B");
    const requestB = harness.memberGets.get(2)!;

    // Newer vault settles first.
    requestB.resolve(jsonResponse(membersB));
    harness.quotaGets
      .find((q) => q.vaultId === 2 && q.method === "GET")!
      .deferred.resolve(
        jsonResponse({
          limits: { storage_soft_limit_bytes: 22 },
          usage: { storage_bytes: 2 },
          evaluation: { state: "evaluated", allowed: true, decisions: [] },
        }),
      );

    await waitFor(() => {
      expect(screen.getByText("B member")).toBeInTheDocument();
    });

    // Stale Vault A arrives later — must not replace B.
    requestA.resolve(jsonResponse(membersA));
    const staleQuota = harness.quotaGets.find(
      (q) => q.vaultId === 1 && q.method === "GET",
    );
    staleQuota?.deferred.resolve(
      jsonResponse({
        limits: { storage_soft_limit_bytes: 11 },
        usage: { storage_bytes: 1 },
        evaluation: { state: "evaluated", allowed: true, decisions: [] },
      }),
    );

    await waitFor(() => {
      expect(screen.getByText("B member")).toBeInTheDocument();
      expect(screen.queryByText("A member")).not.toBeInTheDocument();
    });
    expect(screen.getByLabelText(/storage soft limit/i)).toHaveValue(22);
  });

  it("blocks transfer while a newer vault load is still pending", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin();

    await openVaultMembers(user, "Vault A");
    harness.memberGets.get(1)!.resolve(jsonResponse(membersA));
    harness.quotaGets
      .find((q) => q.vaultId === 1 && q.method === "GET")!
      .deferred.resolve(unlimitedQuotaResponse(1));
    await waitFor(() => {
      expect(screen.getByText("A member")).toBeInTheDocument();
    });

    // Start opening Vault B but leave members pending.
    await user.click(screen.getByRole("button", { name: /close/i }));
    await openVaultMembers(user, "Vault B");

    await user.type(
      screen.getByLabelText(/reason for ownership transfer/i),
      "stale selection",
    );
    await user.type(
      screen.getByLabelText(/type the vault name to confirm/i),
      "Vault B",
    );
    await user.click(
      screen.getByRole("button", { name: /transfer ownership/i }),
    );

    await waitFor(() => {
      expect(
        screen.getByText(/wait for the current vault members/i),
      ).toBeInTheDocument();
    });
    expect(harness.transferPosts.length).toBe(0);
  });
});

describe("AdminPage — reauth replay (seam 9)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("replays a quota save exactly once after successful reauthentication", async () => {
    const user = userEvent.setup();
    const harness = await renderAdmin({
      authMethod: "local",
      quotaPutReauthTimes: 1,
      quotaPutFinal: { status: 422, body: { error: "invalid quota" } },
    });

    await openVaultMembers(user, "Vault A");
    harness.memberGets.get(1)!.resolve(jsonResponse(membersA));
    harness.quotaGets
      .find((q) => q.vaultId === 1 && q.method === "GET")!
      .deferred.resolve(unlimitedQuotaResponse(1));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /save quotas/i }),
      ).toBeEnabled();
    });

    fireEvent.change(screen.getByLabelText(/storage soft limit/i), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByLabelText(/storage hard limit/i), {
      target: { value: "20" },
    });
    fireEvent.change(screen.getByLabelText(/reason for this quota change/i), {
      target: { value: "capacity policy" },
    });
    await user.click(screen.getByRole("button", { name: /save quotas/i }));

    await waitFor(() => {
      const puts = harness.mutatingCalls.filter(
        (c) => c.method === "PUT" && c.url.includes("/quotas"),
      );
      expect(puts.length).toBe(2);
      expect(harness.requestPassword).toHaveBeenCalledTimes(1);
      expect(screen.getByText(/invalid quota/i)).toBeInTheDocument();
    });
    expect(
      screen.getByLabelText(/reason for this quota change/i),
    ).toHaveValue("capacity policy");
  });
});

describe("AdminPage — non-admin redirect (seam 10)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("redirects a viewer or operator away from /admin", async () => {
    const replace = vi.fn();
    const original = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...original, replace, pathname: "/admin", search: "" },
    });

    await renderAdmin({ isAdmin: false });

    await waitFor(() => {
      expect(replace).toHaveBeenCalledWith("/");
    });
    expect(
      screen.queryByRole("heading", { name: /users and vaults/i }),
    ).not.toBeInTheDocument();

    Object.defineProperty(window, "location", {
      configurable: true,
      value: original,
    });
  });
});
