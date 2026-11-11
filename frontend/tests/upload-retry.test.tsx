import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, createAppQueryClient, configureApiClient, resetApiClientForTests } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { UploadRetryActions } from "@/components/UploadRetryActions";
import { jsonResponse, loadCatalog } from "./vault-settings-harness";

describe("explicit failed upload retry", () => {
  afterEach(resetApiClientForTests);
  function show(vaultId?: number, failure = false) {
    const calls: { url: string; body: unknown }[] = [];
    configureApiClient({ fetch: vi.fn(async (input, init) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) return jsonResponse({ locale: "en", locales: ["en", "it"], messages: loadCatalog() });
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
      return failure ? jsonResponse({ detail: "Protection is suspended" }, 409) : jsonResponse({ item_count: 3 });
    }) });
    render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider><UploadRetryActions vaultId={vaultId} /></I18nProvider></ApiQueryProvider>);
    return calls;
  }
  it("offers global retry without a Session-selected Vault or a whole-Vault action", async () => {
    const calls = show();
    await userEvent.click(await screen.findByRole("button", { name: "Retry failed uploads" }));
    await screen.findByText("New upload Jobs queued: 3");
    expect(calls).toEqual([{ url: "/api/upload/retry-failed", body: null }]);
    expect(screen.queryByRole("button", { name: "Upload whole Vault" })).not.toBeInTheDocument();
  });
  it("scopes retry and whole-root upload to the explicit Vault", async () => {
    const calls = show(7);
    await userEvent.click(await screen.findByRole("button", { name: "Retry failed uploads" }));
    await screen.findByText("New upload Jobs queued: 3");
    await userEvent.click(screen.getByRole("button", { name: "Upload whole Vault" }));
    await screen.findByText(loadCatalog()["uploads.whole_started"]);
    expect(calls).toEqual([{ url: "/api/upload/retry-failed?vault_id=7", body: null }, { url: "/api/upload?vault_id=7", body: { path: "", is_directory: true, request_id: expect.any(String) } }]);
  });
  it("reuses the stable request key when an upload response is uncertain", async () => {
    const calls = show(2, true);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Upload whole Vault" }));
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "Upload whole Vault" }));
    await screen.findByRole("alert");
    expect(calls).toHaveLength(2);
    expect(calls[0]).toEqual(calls[1]);
  });
  it("surfaces ineligible errors and reenables retry", async () => {
    show(2, true);
    await userEvent.click(await screen.findByRole("button", { name: "Retry failed uploads" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Protection is suspended"));
    expect(screen.getByRole("button", { name: "Retry failed uploads" })).toBeEnabled();
  });
});
