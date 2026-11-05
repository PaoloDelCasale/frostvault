import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import { FileBrowser } from "@/pages/archive/FileBrowser";
import { jsonResponse } from "./vault-settings-harness";

const file = { type: "file", name: "photo.jpg", path: "2026/photo.jpg", state: "cloud_only", recover_eligible: true, cloud_exists: true, local_exists: false, storage_class: "DEEP_ARCHIVE", cloud_size: 100 };
function setup(items: unknown[] = [file], search = "") {
  window.history.replaceState({}, "", `/${search}`);
  const calls: string[] = [];
  configureApiClient({ fetch: vi.fn(async (input) => {
    const url = String(input); calls.push(url);
    if (url.startsWith("/api/files/detail")) return jsonResponse(file);
    if (url.startsWith("/api/files/versions")) return jsonResponse({ items: [] });
    if (url.startsWith("/api/jobs")) return jsonResponse({ groups: [] });
    return jsonResponse({ items, total: items.length, aggregate_status: "ready" });
  }) });
  render(<ApiQueryProvider client={createAppQueryClient()}><FileBrowser vaultId={1} vaultName="First" t={(key) => key} capabilities={{ delete_enabled: true, cloud_deletion_enabled: true }} /></ApiQueryProvider>);
  return calls;
}
afterEach(() => { resetApiClientForTests(); window.history.replaceState({}, "", "/"); });
describe("essential file browser", () => {
  it("pins browsing and shows only five columns, no cloud internals", async () => {
    const calls = setup(); await screen.findAllByText("photo.jpg");
    expect(calls[0]).toContain("vault_id=1");
    const table = screen.getByTestId("file-list-table");
    expect(within(table).getAllByRole("columnheader")).toHaveLength(6);
    expect(table).not.toHaveTextContent("DEEP_ARCHIVE");
    expect(screen.queryByText(/VersionId/)).not.toBeInTheDocument();
  });
  it("searches all protected Vaults with the Cloud-only filter and retains result identity", async () => {
    const calls = setup([{ ...file, vault_id: 2, vault_name: "Second" }], "?q=photo&state=cloud_only");
    await screen.findAllByText("Second / 2026/photo.jpg");
    expect(calls[0]).toContain("/api/files/search?"); expect(calls[0]).toContain("state=cloud_only"); expect(calls[0]).not.toContain("vault_id");
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("checkbox")[0]);
    await user.click(screen.getByRole("button", { name: "essential.details" }));
    await waitFor(() => expect(calls.some((url) => url.startsWith("/api/files/detail?") && url.endsWith("vault_id=2"))).toBe(true));
    expect(calls.some((url) => url.includes("/select"))).toBe(false);
  });
  it("folder rows show state counts, never Mixed", async () => {
    setup([{ type: "directory", name: "2026", path: "2026", state: "problem", item_count: 7, total_size: 99, state_counts: { protected: 5, cloud_only: 2 } }]);
    await screen.findAllByText("5 state.protected · 2 state.cloud_only");
    expect(screen.queryByText(/mixed|misto/i)).not.toBeInTheDocument();
  });
  it("email link loads a file outside the first page, highlights it and opens the problem details", async () => {
    const calls = setup([], "?vault_id=1&directory=2026&file=2026%2Fphoto.jpg");
    expect(await screen.findByRole("dialog")).toHaveTextContent("First / 2026/photo.jpg");
    expect(document.querySelector('[data-highlighted="true"]')).not.toBeNull();
    expect(calls.some((url) => url.startsWith("/api/files/detail?path=2026%2Fphoto.jpg&vault_id=1"))).toBe(true);
  });
  it("resets selection when a filter changes and offers no bulk cloud deletion", async () => {
    setup(); await screen.findAllByText("photo.jpg"); const user = userEvent.setup();
    await user.click(screen.getAllByRole("checkbox")[0]);
    expect(screen.getByRole("button", { name: "ui.row_action_recover" })).toBeEnabled();
    await user.selectOptions(screen.getByTestId("state-filter"), "cloud_only");
    expect(screen.getByRole("button", { name: "ui.row_action_recover" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /cloud.purge/ })).not.toBeInTheDocument();
  });
});
