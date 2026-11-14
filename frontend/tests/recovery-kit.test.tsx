import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { RecoveryKitSection } from "@/pages/general/RecoveryKitSection";

const messages = JSON.parse(readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app/locales/en.json"), "utf8"));
function json(data: unknown, status = 200) { return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } }); }
function renderSection() {
  render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider><RecoveryKitSection /></I18nProvider></ApiQueryProvider>);
}
afterEach(() => { resetApiClientForTests(); vi.restoreAllMocks(); });

describe("Installation Recovery Kit", () => {
  it("shows one fingerprint-bound custody action and a durable dated file-count warning", async () => {
    const fingerprint = "a".repeat(24);
    const status = { configured: true, fingerprint, custody_acknowledged: false, acknowledged_at: null,
      metadata_restore: { backup_created_at: "2026-11-02T10:00:00Z", updated_files: 42 } };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) return json({ locale: "en", locales: ["en", "it"], messages });
      if (url === "/api/recovery-kit/custody") {
        expect(JSON.parse(String(init?.body))).toEqual({ fingerprint });
        return json({ ...status, custody_acknowledged: true, acknowledged_at: "2026-11-03" });
      }
      if (url === "/api/recovery-kit") return json(status);
      throw new Error(`Unexpected request ${url}`);
    });
    configureApiClient({ fetch: fetchMock, csrfToken: "csrf" });
    renderSection();
    expect(await screen.findByText(fingerprint)).toBeInTheDocument();
    const warning = screen.getByTestId("metadata-restore-warning");
    expect(warning).toHaveTextContent(/Nov 2, 2026, \d{1,2}:\d{2}\s?[AP]M/);
    expect(warning).not.toHaveTextContent("2026-11-02T");
    expect(warning).toHaveTextContent("42 files updated");
    await userEvent.click(screen.getByRole("button", { name: /I have stored/i }));
    expect(await screen.findByText("Custody confirmed for the current kit.")).toBeInTheDocument();
    expect(screen.getByTestId("metadata-restore-warning")).toBeInTheDocument();
  });

  it("reports stale custody without displaying or logging any kit material", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) return json({ locale: "en", locales: ["en", "it"], messages });
      if (url.endsWith("/custody")) return json({ detail: "fingerprint changed" }, 409);
      return json({ configured: true, fingerprint: "b".repeat(24), custody_acknowledged: false, acknowledged_at: null, metadata_restore: null });
    });
    configureApiClient({ fetch: fetchMock, csrfToken: "csrf" });
    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: /I have stored/i }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("The kit changed"));
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/download"))).toBe(false);
  });
});
