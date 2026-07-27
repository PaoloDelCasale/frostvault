import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { InvitesPanel } from "@/pages/admin/InvitesPanel";

const localesDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app/locales");
const messages = JSON.parse(readFileSync(path.join(localesDir, "en.json"), "utf8")) as Record<string, string>;
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => {
  cleanup();
  localStorage.clear();
  resetApiClientForTests();
});

it("lists pending Invites with expiry and without credential material", async () => {
  configureApiClient({
    csrfToken: "csrf",
    fetch: vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) return json({ locale: "en", locales: ["en", "it"], messages });
      if (url === "/api/admin/invites" && (init?.method ?? "GET") === "GET") {
        return json({
          items: [
            {
              id: 9,
              target_user_id: 20,
              target_username: "bob",
              created_by: 1,
              expires_at: "2026-08-01T12:00:00+00:00",
              token: "must-never-appear",
              token_hash: "also-must-never-appear",
            },
          ],
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    }),
  });

  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <InvitesPanel users={[{ id: 20, username: "bob", display_name: "Bob", active: true, is_admin: false, vault_count: 0, has_password: false, identity_count: 0 }]} />
      </I18nProvider>
    </ApiQueryProvider>,
  );

  expect(await screen.findByText("@bob")).toBeInTheDocument();
  expect(screen.getByText(/Expires 2026-08-01T12:00:00\+00:00/i)).toBeInTheDocument();
  expect(screen.queryByText("must-never-appear")).not.toBeInTheDocument();
  expect(screen.queryByText("also-must-never-appear")).not.toBeInTheDocument();
  expect(screen.queryByText(/no pending invites/i)).not.toBeInTheDocument();
});

it("revokes a pending Invite after confirmation and removes it from the list", async () => {
  const user = userEvent.setup();
  let items = [
    {
      id: 9,
      target_user_id: 20,
      target_username: "bob",
      created_by: 1,
      expires_at: "2026-08-01T12:00:00+00:00",
    },
  ];
  const revocations: string[] = [];
  configureApiClient({
    csrfToken: "csrf",
    fetch: vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) return json({ locale: "en", locales: ["en", "it"], messages });
      if (url === "/api/admin/invites" && (init?.method ?? "GET") === "GET") return json({ items });
      if (url === "/api/admin/invites/9/revoke" && init?.method === "POST") {
        revocations.push(url);
        items = [];
        return json({});
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    }),
  });

  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <InvitesPanel users={[{ id: 20, username: "bob", display_name: "Bob", active: true, is_admin: false, vault_count: 0, has_password: false, identity_count: 0 }]} />
      </I18nProvider>
    </ApiQueryProvider>,
  );

  expect(await screen.findByText("@bob")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /^revoke$/i }));
  const confirmation = await screen.findByRole("alertdialog");
  await user.click(screen.getByRole("button", { name: /confirm revoke/i }));

  await waitFor(() => expect(revocations).toEqual(["/api/admin/invites/9/revoke"]));
  expect(screen.queryByText("@bob")).not.toBeInTheDocument();
  expect(await screen.findByText(/no pending invites/i)).toBeInTheDocument();
  expect(confirmation).not.toBeInTheDocument();
});

it("shows newly issued Invite material once without persisting it", async () => {
  const user = userEvent.setup();
  const token = "one-time-invite-credential";
  const mutations: unknown[] = [];
  configureApiClient({
    csrfToken: "csrf",
    fetch: vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) return json({ locale: "en", locales: ["en", "it"], messages });
      if (url === "/api/admin/invites" && (init?.method ?? "GET") === "GET") return json({ items: [] });
      if (url === "/api/admin/invites" && init?.method === "POST") {
        mutations.push(JSON.parse(String(init.body)));
        return json({ token }, 201);
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    }),
  });

  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <InvitesPanel users={[{ id: 20, username: "bob", display_name: "Bob", active: true, is_admin: false, vault_count: 0, has_password: false, identity_count: 0 }]} />
      </I18nProvider>
    </ApiQueryProvider>,
  );

  await screen.findByText(/no pending invites/i);
  await user.selectOptions(screen.getByLabelText(/invite user/i), "20");
  await user.click(screen.getByRole("button", { name: /create invite/i }));

  expect(await screen.findByText(token)).toBeInTheDocument();
  expect(mutations).toEqual([{ target_user_id: 20 }]);
  expect(JSON.stringify(localStorage)).not.toContain(token);
  await user.click(screen.getByRole("button", { name: /i have saved the invite/i }));
  await waitFor(() => expect(screen.queryByText(token)).not.toBeInTheDocument());
});

it("copies issued Invite material to the clipboard without persisting it", async () => {
  const user = userEvent.setup();
  const token = "copy-me-invite-credential";
  const writeText = vi.fn(async () => undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  configureApiClient({
    csrfToken: "csrf",
    fetch: vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) return json({ locale: "en", locales: ["en", "it"], messages });
      if (url === "/api/admin/invites" && (init?.method ?? "GET") === "GET") return json({ items: [] });
      if (url === "/api/admin/invites" && init?.method === "POST") return json({ token }, 201);
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    }),
  });

  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <InvitesPanel users={[{ id: 20, username: "bob", display_name: "Bob", active: true, is_admin: false, vault_count: 0, has_password: false, identity_count: 0 }]} />
      </I18nProvider>
    </ApiQueryProvider>,
  );

  await screen.findByText(/no pending invites/i);
  await user.selectOptions(screen.getByLabelText(/invite user/i), "20");
  await user.click(screen.getByRole("button", { name: /create invite/i }));
  expect(await screen.findByText(token)).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: /copy invite/i }));
  expect(writeText).toHaveBeenCalledWith(token);
  expect(JSON.stringify(localStorage)).not.toContain(token);
});
