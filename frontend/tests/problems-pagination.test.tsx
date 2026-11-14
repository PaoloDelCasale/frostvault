import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests, type ApiFetch } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { ProblemsPage } from "@/pages/general/ProblemsPage";
import { jsonResponse, loadCatalog } from "./vault-settings-harness";

const en = loadCatalog("en");
const problem = (id: number) => ({ id, title: `Problem ${id}`, body: "body", vault_id: 1, message_params: {} });

function renderWith(problems: { items: unknown[]; open_problem_count: number }) {
  resetApiClientForTests();
  configureApiClient({ fetch: vi.fn<ApiFetch>(async (input) => String(input).startsWith("/api/i18n/catalog")
    ? jsonResponse({ locale: "en", locales: ["en", "it"], messages: en })
    : jsonResponse(problems)) });
  render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider><ProblemsPage /></I18nProvider></ApiQueryProvider>);
}

describe("Problems pagination (#650)", () => {
  beforeEach(() => resetApiClientForTests());

  it("hides Previous/Next with no Problems", async () => {
    renderWith({ items: [], open_problem_count: 0 });
    expect(await screen.findByText(en["essential.no_problems"])).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: en["ui.next"] })).not.toBeInTheDocument();
  });

  it("hides Previous/Next with a single page", async () => {
    renderWith({ items: [problem(1), problem(2)], open_problem_count: 2 });
    expect(await screen.findByText("Problem 2")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: en["ui.previous"] })).not.toBeInTheDocument();
  });

  it("offers Next when more Problems exist than one page", async () => {
    renderWith({ items: Array.from({ length: 100 }, (_, index) => problem(index + 1)), open_problem_count: 150 });
    expect(await screen.findByRole("button", { name: en["ui.next"] })).toBeEnabled();
    expect(screen.getByRole("button", { name: en["ui.previous"] })).toBeDisabled();
  });
});
