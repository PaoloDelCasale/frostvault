import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { IdentityDialog } from "@/pages/admin/IdentityDialog";

const localesDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app/locales");
const messages = JSON.parse(readFileSync(path.join(localesDir, "en.json"), "utf8")) as Record<string, string>;
function response(data: unknown) {
  return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  cleanup();
  resetApiClientForTests();
});

it("inspects and explicitly unlinks an Identity through the User boundary", async () => {
  const user = userEvent.setup();
  const mutations: string[] = [];
  let identities = [{ id: 8, issuer: "https://idp.example", subject: "stable-subject", created_at: "2026-07-01T00:00:00Z" }];
  configureApiClient({
    csrfToken: "csrf",
    fetch: vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) return response({ locale: "en", locales: ["en", "it"], messages });
      if (url === "/api/admin/users/20/identities" && (init?.method ?? "GET") === "GET") return response({ items: identities });
      if (url === "/api/admin/users/20/identities/8?confirm=true" && init?.method === "DELETE") {
        mutations.push(url);
        identities = [];
        return response({ items: [] });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    }),
  });

  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <IdentityDialog open onOpenChange={() => undefined} userId={20} userName="B user" />
      </I18nProvider>
    </ApiQueryProvider>,
  );

  expect(await screen.findByText("https://idp.example")).toBeInTheDocument();
  expect(screen.getByText("stable-subject")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /unlink identity/i }));
  const confirmation = await screen.findByRole("alertdialog");
  await user.click(screen.getByRole("button", { name: /confirm unlink/i }));

  await waitFor(() => expect(mutations).toEqual(["/api/admin/users/20/identities/8?confirm=true"]));
  expect(screen.queryByText("stable-subject")).not.toBeInTheDocument();
  expect(confirmation).not.toBeInTheDocument();
});
