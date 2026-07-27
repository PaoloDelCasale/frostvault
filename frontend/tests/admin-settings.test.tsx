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
import { SettingsSection } from "@/pages/admin/SettingsSection";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);
const messages = JSON.parse(
  readFileSync(path.join(localesDir, "en.json"), "utf8"),
) as Record<string, string>;

function jsonResponse(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  cleanup();
  resetApiClientForTests();
});

it("shows effective defaults with provenance and never renders secret values", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    }
    if (url === "/api/admin/settings") {
      return jsonResponse({
        revision: 4,
        groups: {
          security: [
            {
              key: "archive_master_key",
              environment_variable: "ARCHIVE_MASTER_KEY",
              source: "environment",
              mutability: "deployment_only",
              restart_required: true,
              configured: true,
              effective_value: "must-never-reach-the-dom",
            },
          ],
          oidc: [],
          operations: [
            {
              key: "scan_interval",
              environment_variable: "SCAN_INTERVAL_SECONDS",
              source: "environment_default",
              mutability: "runtime_managed",
              restart_required: false,
              effective_value: 1800,
              minimum: 30,
              maximum: 86400,
            },
          ],
          restore: [],
          vault_defaults: [],
        },
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  });
  configureApiClient({ fetch: fetchMock });

  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <SettingsSection mode="defaults" />
      </I18nProvider>
    </ApiQueryProvider>,
  );

  expect(await screen.findByLabelText("SCAN_INTERVAL_SECONDS")).toHaveValue(1800);
  expect(screen.getByText(/environment default/i)).toBeInTheDocument();
  expect(screen.queryByText("must-never-reach-the-dom")).not.toBeInTheDocument();
});

it("updates a runtime-managed default with the displayed revision", async () => {
  const user = userEvent.setup();
  const calls: Array<{ method: string; body: string }> = [];
  const setting = {
    key: "scan_interval",
    environment_variable: "SCAN_INTERVAL_SECONDS",
    source: "built_in_default",
    mutability: "runtime_managed",
    restart_required: false,
    effective_value: 1800,
    minimum: 30,
    maximum: 86400,
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    }
    if (url === "/api/admin/settings" && (init?.method ?? "GET") === "GET") {
      return jsonResponse({ revision: 7, groups: { operations: [setting] } });
    }
    if (url === "/api/admin/settings" && init?.method === "PATCH") {
      calls.push({ method: init.method, body: String(init.body) });
      return jsonResponse({
        revision: 8,
        groups: {
          operations: [{ ...setting, effective_value: 2400, source: "database_override" }],
        },
      });
    }
    throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
  });
  configureApiClient({ fetch: fetchMock, csrfToken: "csrf" });

  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <SettingsSection mode="defaults" />
      </I18nProvider>
    </ApiQueryProvider>,
  );

  const input = await screen.findByLabelText("SCAN_INTERVAL_SECONDS");
  await user.clear(input);
  await user.type(input, "2400");
  await user.click(screen.getByRole("button", { name: /save defaults/i }));

  await waitFor(() => expect(calls).toHaveLength(1));
  expect(JSON.parse(calls[0]!.body)).toEqual({
    revision: 7,
    overrides: { scan_interval: 2400 },
    removals: [],
  });
  expect(screen.getByLabelText("SCAN_INTERVAL_SECONDS")).toHaveValue(2400);
});
