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
