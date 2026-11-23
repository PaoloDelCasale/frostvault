import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { configureApiClient, resetApiClientForTests } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { ProblemsPage } from "@/pages/general/ProblemsPage";
import { jsonResponse } from "./support/http";
import { renderWithQueryClient } from "./support/queryClient";

const locales = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app/locales");
const en = JSON.parse(readFileSync(path.join(locales, "en.json"), "utf8")) as Record<string, string>;
const problem = { id: 5, user_id: 1, vault_id: 2, job_id: null, event: "upload_failed", title: "Upload failed", body: "Check the bucket.", title_key: null, body_key: null,
  message_params: { path: "2026/photo.jpg" }, in_app_enabled: true, dedupe_key: null, created_at: "2026-11-14T09:30:00Z", read: false, read_at: null, problem_status: "open" };

function renderPage(problems: () => Response) {
  document.documentElement.lang = "en";
  configureApiClient({ fetch: vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) return jsonResponse({ locale: "en", locales: ["en", "it"], messages: en });
    if (url.startsWith("/api/problems")) return problems();
    throw new Error(`Unexpected request ${url}`);
  }) });
  renderWithQueryClient(
    <I18nProvider><ProblemsPage vaults={[{ id: 2, name: "Photos" }]} /></I18nProvider>,
    { config: { defaultOptions: { queries: { retry: false } } } },
  );
}
afterEach(() => { cleanup(); resetApiClientForTests(); });

describe("Problems page (#657)", () => {
  it("names the Vault, formats the date and labels each link with its problem", async () => {
    renderPage(() => jsonResponse({ items: [problem, { ...problem, id: 6, vault_id: null, title: "Cloud unreachable", message_params: {} }], unread_count: 0 }));
    const [row, system] = await screen.findAllByTestId("problem-row");
    expect(row).toHaveTextContent("Vault: Photos");
    const time = within(row!).getByText((_, node) => node?.tagName === "TIME");
    expect(time).toHaveAttribute("dateTime", problem.created_at);
    expect(time.textContent).not.toBe(problem.created_at);
    expect(within(row!).getByRole("link", { name: "Open problem and remedy: Upload failed" })).toHaveAttribute("href", expect.stringContaining("/vaults/2/files"));
    expect(system).toHaveTextContent(en["essential.problem_system"]!);
    expect(within(system!).getByRole("link", { name: "Open problem and remedy: Cloud unreachable" })).toHaveAttribute("href", "/system");
  });

  it("offers Retry when the problems cannot be loaded", async () => {
    let fail = true;
    renderPage(() => fail ? jsonResponse({ detail: "boom" }, 500) : jsonResponse({ items: [problem], unread_count: 0 }));
    const alert = await screen.findByTestId("problems-error");
    expect(alert).toHaveTextContent(/\S/);
    fail = false;
    await userEvent.setup().click(within(alert).getByRole("button", { name: en["ui.retry"] }));
    expect(await screen.findByTestId("problem-row")).toBeInTheDocument();
  });

  it("says when a running scan can close a blocked protection by itself (#711)", async () => {
    renderPage(() => jsonResponse({ items: [{ ...problem, event: "protection_blocked", resolving: "scan" }, { ...problem, id: 7 }], unread_count: 0 }));
    const [blocked, other] = await screen.findAllByTestId("problem-row");
    expect(within(blocked!).getByTestId("problem-resolving")).toHaveTextContent(en["problems.resolving_scan"]!);
    expect(within(other!).queryByTestId("problem-resolving")).toBeNull();
  });
});
