import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
  type ApiFetch,
} from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { CostPriceBooksSection } from "@/pages/admin/CostPriceBooksSection";

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

const builtin = {
  id: null,
  name: "builtin-defaults",
  currency: "EUR",
  effective_at: "2026-01-01T00:00:00+00:00",
  updated_at: null,
  assumptions: { region: "eu-south-1", unit: "EUR per GiB" },
  storage_rates: { STANDARD: 0.023 },
  restore_rates: { GLACIER: { Bulk: 0.0025 } },
  is_active: true,
};

const inactive = {
  id: 7,
  name: "July rates",
  currency: "EUR",
  effective_at: "2026-07-01T00:00:00+00:00",
  updated_at: "2026-06-20T10:00:00+00:00",
  assumptions: { region: "eu-south-1", source: "operator" },
  storage_rates: { CUSTOM_STORAGE: 0.01 },
  restore_rates: { CUSTOM_STORAGE: { AnyTier: 0.02 } },
  is_active: false,
};

function renderSection(fetchMock: ApiFetch) {
  configureApiClient({ fetch: fetchMock, csrfToken: "csrf" });
  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <CostPriceBooksSection />
      </I18nProvider>
    </ApiQueryProvider>,
  );
}

afterEach(() => {
  cleanup();
  resetApiClientForTests();
});

it("shows the builtin active book even when persistence lists only inactive books", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    }
    if (url === "/api/admin/cost-price-books") {
      return jsonResponse({ items: [inactive] });
    }
    if (url === "/api/admin/cost-price-books/active") {
      return jsonResponse(builtin);
    }
    throw new Error(`Unexpected request: ${url}`);
  });

  renderSection(fetchMock);

  expect(await screen.findByTestId("active-price-book")).toHaveTextContent(
    "builtin-defaults",
  );
  expect(screen.getByText("Unavailable (built-in fallback)")).toBeInTheDocument();
  expect(await screen.findByText("July rates")).toBeInTheDocument();
  expect(screen.getAllByText(/CUSTOM_STORAGE/)).not.toHaveLength(0);
  expect(screen.getByRole("button", { name: "Activate price book July rates" })).toBeInTheDocument();
});

it("creates arbitrary JSON rate maps without activating the new book", async () => {
  const user = userEvent.setup();
  let createBody = "";
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    }
    if (url === "/api/admin/cost-price-books/active") return jsonResponse(builtin);
    if (url === "/api/admin/cost-price-books" && method === "GET") {
      return jsonResponse({ items: [] });
    }
    if (url === "/api/admin/cost-price-books" && method === "POST") {
      createBody = String(init?.body);
      return jsonResponse({ ...inactive, id: 8, name: "Custom", is_active: false }, 201);
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  });

  renderSection(fetchMock);
  await screen.findByTestId("active-price-book");

  await user.clear(screen.getByLabelText("Name"));
  await user.type(screen.getByLabelText("Name"), "Custom");
  await user.clear(screen.getByLabelText(/^Audit reason/));
  await user.type(screen.getByLabelText(/^Audit reason/), "monthly rate update");
  const assumptionsEditor = screen.getByRole("textbox", { name: /^Assumptions/ });
  const storageRatesEditor = screen.getByRole("textbox", { name: /^Storage rates/ });
  const restoreRatesEditor = screen.getByRole("textbox", { name: /^Restore rates/ });
  fireEvent.change(assumptionsEditor, { target: { value: '{"region":"custom","flag":true}' } });
  fireEvent.change(storageRatesEditor, { target: { value: '{"MY_TIER":0.0042}' } });
  fireEvent.change(restoreRatesEditor, { target: { value: '{"MY_TIER":{"Weekend":0.11}}' } });
  await user.click(screen.getByRole("button", { name: "Create price book" }));

  await waitFor(() => expect(createBody).not.toBe(""));
  expect(JSON.parse(createBody)).toMatchObject({
    assumptions: { region: "custom", flag: true },
    storage_rates: { MY_TIER: 0.0042 },
    restore_rates: { MY_TIER: { Weekend: 0.11 } },
    reason: "monthly rate update",
  });
  expect(fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/activate"))).toHaveLength(0);
});

it("requires an audit reason before activating an inactive persisted book", async () => {
  const user = userEvent.setup();
  let active: typeof builtin | typeof inactive = builtin;
  let activationBody = "";
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    }
    if (url === "/api/admin/cost-price-books" && method === "GET") {
      return jsonResponse({ items: [active === builtin ? inactive : { ...inactive, is_active: true }] });
    }
    if (url === "/api/admin/cost-price-books/active") return jsonResponse(active);
    if (url === "/api/admin/cost-price-books/7/activate") {
      activationBody = String(init?.body);
      active = { ...inactive, is_active: true };
      return jsonResponse(active);
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  });

  renderSection(fetchMock);
  await screen.findByTestId("active-price-book");
  await user.click(screen.getByRole("button", { name: "Activate price book July rates" }));

  expect(screen.getByText(/changes the storage and recovery estimates displayed to users/i)).toBeInTheDocument();
  const confirm = screen.getByRole("button", { name: "Confirm activation" });
  expect(confirm).toBeDisabled();
  await user.type(screen.getByLabelText(/^Activation audit reason/), "activate July rates");
  expect(confirm).toBeEnabled();
  await user.click(confirm);

  await waitFor(() => expect(JSON.parse(activationBody)).toEqual({ reason: "activate July rates" }));
  expect(new Headers(fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/activate"))?.[1]?.headers).get("X-CSRF-Token")).toBe("csrf");
});

it("keeps the activation dialog and reason after an activation failure", async () => {
  const user = userEvent.setup();
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    }
    if (url === "/api/admin/cost-price-books" && method === "GET") {
      return jsonResponse({ items: [inactive] });
    }
    if (url === "/api/admin/cost-price-books/active") return jsonResponse(builtin);
    if (url === "/api/admin/cost-price-books/7/activate") {
      return jsonResponse({ detail: "activation failed" }, 500);
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  });

  renderSection(fetchMock);
  await screen.findByTestId("active-price-book");
  await user.click(screen.getByRole("button", { name: "Activate price book July rates" }));
  const reason = screen.getByLabelText(/^Activation audit reason/);
  await user.type(reason, "keep this reason");
  await user.click(screen.getByRole("button", { name: "Confirm activation" }));

  await waitFor(() => expect(screen.getByText("activation failed")).toBeInTheDocument());
  expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  expect(screen.getByLabelText(/^Activation audit reason/)).toHaveValue("keep this reason");
  expect(screen.getByRole("button", { name: "Confirm activation" })).toBeEnabled();
});
