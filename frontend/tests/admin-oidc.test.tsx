import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { OidcSection } from "@/pages/admin/OidcSection";

const localesDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app/locales");
const messages = JSON.parse(readFileSync(path.join(localesDir, "en.json"), "utf8")) as Record<string, string>;
const json = (data: unknown) => new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });

afterEach(() => {
  cleanup();
  resetApiClientForTests();
});

it("saves, validates, and activates a redacted OIDC draft", async () => {
  const user = userEvent.setup();
  const secret = "write-only-client-secret";
  const bodies: unknown[] = [];
  const active = {
    enabled: false,
    issuer: "",
    client_id: "",
    client_secret_configured: false,
    scopes: ["openid"],
    login_transaction_ttl_seconds: 300,
    callback_url: "https://vault.example/auth/oidc/callback",
    source: "environment",
  };
  let state: Record<string, unknown> = { active, draft: null, configuration_status: "disabled", last_validation: null };
  configureApiClient({
    csrfToken: "csrf",
    fetch: vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) return json({ locale: "en", locales: ["en", "it"], messages });
      if (url === "/api/admin/oidc-configuration" && (init?.method ?? "GET") === "GET") return json(state);
      if (url.endsWith("/draft") && init?.method === "PUT") {
        bodies.push(JSON.parse(String(init.body)));
        state = { ...state, configuration_status: "draft", draft: { issuer: "https://idp.example", client_id: "frostvault", client_secret_configured: true, scopes: ["openid", "profile"], login_transaction_ttl_seconds: 300, version: 1, validation_status: "not_validated", client_secret: "malicious-response-leak" } };
        return json(state);
      }
      if (url.endsWith("/draft/validate") && init?.method === "POST") {
        state = { ...state, configuration_status: "validated", draft: { ...(state.draft as object), validation_status: "valid" }, last_validation: { status: "valid", validated_at: "2026-01-01T00:00:00Z" } };
        return json(state);
      }
      if (url.endsWith("/activate") && init?.method === "POST") {
        state = { ...state, configuration_status: "active", active: { ...active, enabled: true, issuer: "https://idp.example", client_id: "frostvault", client_secret_configured: true } };
        return json(state);
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    }),
  });

  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider><OidcSection /></I18nProvider>
    </ApiQueryProvider>,
  );

  expect(await screen.findByText("https://vault.example/auth/oidc/callback")).toBeInTheDocument();
  await user.type(screen.getByLabelText(/^issuer$/i), "https://idp.example");
  await user.type(screen.getByLabelText(/client id/i), "frostvault");
  await user.type(document.querySelector("#oidc-client-secret") as HTMLInputElement, secret);
  await user.clear(screen.getByLabelText(/scopes/i));
  await user.type(screen.getByLabelText(/scopes/i), "openid profile");
  await user.click(screen.getByRole("button", { name: /save draft/i }));

  await waitFor(() => expect(bodies).toHaveLength(1));
  expect(bodies[0]).toEqual({ issuer: "https://idp.example", client_id: "frostvault", client_secret: secret, scopes: ["openid", "profile"], login_transaction_ttl_seconds: 300 });
  expect(screen.queryByDisplayValue(secret)).not.toBeInTheDocument();
  expect(screen.queryByText("malicious-response-leak")).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: /validate draft/i }));
  expect(await screen.findByRole("status")).toHaveTextContent(/validated/i);
  await user.click(screen.getByRole("button", { name: /activate oidc/i }));
  expect(await screen.findByText(/OIDC is active/i)).toBeInTheDocument();
});
