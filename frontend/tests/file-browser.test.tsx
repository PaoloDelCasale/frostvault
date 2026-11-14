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
    if (url.startsWith("/api/stats")) return jsonResponse({ states: { protected: 1180, cloud_only: 3, pending: 2 }, protection_state: "pending", storage: {}, active_jobs: 0, runtime: {}, source_volume: {}, delete_enabled: true });
    return jsonResponse({ items, total: items.length, aggregate_status: "ready" });
  }) });
  render(<ApiQueryProvider client={createAppQueryClient()}><FileBrowser vaultId={1} vaultName="First" t={(key) => key} capabilities={{ delete_enabled: true, cloud_deletion_enabled: true }} /></ApiQueryProvider>);
  return calls;
}
afterEach(() => { resetApiClientForTests(); window.history.replaceState({}, "", "/"); });
const select = (name: RegExp) => screen.getAllByRole("checkbox", { name })[0];
describe("essential file browser", () => {
  it("pins browsing to the Vault and shows name, state, size and modified only", async () => {
    const calls = setup(); await screen.findAllByText("photo.jpg");
    expect(calls.find((url) => url.startsWith("/api/files?"))).toContain("vault_id=1");
    const table = screen.getByTestId("file-list-table");
    expect(within(table).getAllByRole("columnheader")).toHaveLength(5);
    expect(table).not.toHaveTextContent("essential.ready");
    expect(table).not.toHaveTextContent("DEEP_ARCHIVE");
    expect(screen.queryByText(/VersionId/)).not.toBeInTheDocument();
  });
  it("names the Vault with its state and the Files | Settings tabs", async () => {
    setup(); const header = await screen.findByTestId("vault-header");
    expect(within(header).getByRole("heading", { level: 1, name: "First" })).toBeInTheDocument();
    expect(await within(header).findByText("state.pending")).toBeInTheDocument();
    expect(within(header).getByRole("link", { name: "nav.files" })).toHaveAttribute("aria-current", "page");
    expect(within(header).getByRole("link", { name: "nav.settings" })).toHaveAttribute("href", "/vaults/1/settings");
  });
  it("filters by state chips that carry the Vault-wide counts", async () => {
    const calls = setup(); await screen.findAllByText("photo.jpg");
    const chips = screen.getByTestId("state-filter");
    expect(await within(chips).findByRole("button", { name: "files.chip_all 1,185" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.setup().click(within(chips).getByRole("button", { name: "files.chip_cloud_only 3" }));
    await waitFor(() => expect(calls.some((url) => url.startsWith("/api/files?") && url.includes("state=cloud_only"))).toBe(true));
    expect(window.location.search).toContain("state=cloud_only");
  });
  it("searches this Vault by default and every Vault on request", async () => {
    const calls = setup([file], "?q=photo");
    await screen.findAllByText("2026/photo.jpg");
    expect(calls.find((url) => url.startsWith("/api/files?q=photo"))).toContain("vault_id=1");
    expect(calls.some((url) => url.startsWith("/api/files/search"))).toBe(false);
    await userEvent.setup().click(within(screen.getByTestId("search-scope")).getByRole("button", { name: "files.scope_all" }));
    await waitFor(() => expect(calls.some((url) => url.startsWith("/api/files/search?q=photo"))).toBe(true));
  });
  it("searches all Vaults with the Cloud-only filter and retains result identity", async () => {
    const calls = setup([{ ...file, vault_id: 2, vault_name: "Second" }], "?q=photo&state=cloud_only&scope=all");
    await screen.findAllByText("Second / 2026/photo.jpg");
    const search = calls.find((url) => url.startsWith("/api/files/search?"));
    expect(search).toContain("state=cloud_only"); expect(search).not.toContain("vault_id");
    const user = userEvent.setup();
    await user.click(select(/essential.select Second/));
    await user.click(within(screen.getByTestId("selection-bar")).getByRole("button", { name: "essential.details" }));
    await waitFor(() => expect(calls.some((url) => url.startsWith("/api/files/detail?") && url.endsWith("vault_id=2"))).toBe(true));
    expect(calls.some((url) => url.includes("/select"))).toBe(false);
  });
  it("folder rows show a badge per state, never Mixed", async () => {
    setup([{ type: "directory", name: "2026", path: "2026", state: "problem", item_count: 7, total_size: 99, state_counts: { protected: 5, cloud_only: 2 } }]);
    expect((await screen.findAllByText("state.protected 5"))[0]).toHaveAttribute("data-state", "protected");
    expect(screen.getAllByText("state.cloud_only 2")[0]).toHaveAttribute("data-state", "cloud_only");
    expect(screen.queryByText(/mixed|misto/i)).not.toBeInTheDocument();
  });
  it("explains Stopped Uploads of the level and uploads their files and folders now (#630)", async () => {
    const calls = setup([
      { type: "directory", name: "2026", path: "2026", state: "stopped", item_count: 7, total_size: 99, state_counts: { protected: 4, stopped: 3 } },
      { type: "directory", name: "2025", path: "2025", state: "protected", item_count: 2, total_size: 9, state_counts: { protected: 2 } },
      { ...file, name: "held.jpg", path: "held.jpg", state: "stopped", local_exists: true, upload_eligible: true },
    ]);
    expect((await screen.findAllByText("state.stopped 3"))[0]).toHaveAttribute("data-state", "stopped");
    const notice = screen.getByTestId("stopped-uploads");
    expect(notice).toHaveTextContent("files.stopped_notice");
    await userEvent.setup().click(within(notice).getByRole("button", { name: "essential.upload_now" }));
    await waitFor(() => expect(calls.filter((url) => url === "/api/upload?vault_id=1")).toHaveLength(2));
    expect(within(screen.getByTestId("state-filter")).getByRole("button", { name: /files.chip_stopped/ })).toBeInTheDocument();
  });
  it("shows no Stopped Upload notice without one", async () => {
    setup(); await screen.findAllByText("photo.jpg");
    expect(screen.queryByTestId("stopped-uploads")).not.toBeInTheDocument();
  });
  it("roots the breadcrumb at the Vault name", async () => {
    setup([file], "?directory=2026%2Fsummer");
    const crumbs = await screen.findByTestId("breadcrumbs");
    expect(within(crumbs).getByRole("button", { name: "First" })).toHaveAttribute("data-directory", "");
    expect(within(crumbs).getByRole("button", { name: "summer" })).toHaveAttribute("aria-current", "location");
  });
  it("email link loads a file outside the first page, highlights it and opens its details panel", async () => {
    const calls = setup([], "?vault_id=1&directory=2026&file=2026%2Fphoto.jpg");
    expect(await screen.findByRole("dialog")).toHaveTextContent("First / 2026/photo.jpg");
    expect(document.querySelector('[data-highlighted="true"]')).not.toBeNull();
    expect(calls.some((url) => url.startsWith("/api/files/detail?path=2026%2Fphoto.jpg&vault_id=1"))).toBe(true);
  });
  it("shows the selection bar only with a selection, and clears it when a filter changes", async () => {
    setup(); await screen.findAllByText("photo.jpg"); const user = userEvent.setup();
    expect(screen.queryByTestId("selection-bar")).not.toBeInTheDocument();
    await user.click(select(/essential.select First/));
    const bar = screen.getByTestId("selection-bar");
    expect(bar).toHaveTextContent("files.selected");
    expect(within(bar).getByRole("button", { name: "ui.row_action_recover" })).toBeEnabled();
    expect(within(bar).getByRole("button", { name: "essential.free_now" })).toBeDisabled();
    expect(within(bar).queryByRole("button", { name: /cloud.purge/ })).not.toBeInTheDocument();
    await user.click(within(screen.getByTestId("state-filter")).getByRole("button", { name: /files.chip_pending/ }));
    expect(screen.queryByTestId("selection-bar")).not.toBeInTheDocument();
    await user.click(select(/essential.select First/));
    await user.click(within(screen.getByTestId("selection-bar")).getByRole("button", { name: "files.clear_selection" }));
    expect(screen.queryByTestId("selection-bar")).not.toBeInTheDocument();
  });
  it("confirms free space as a count with a collapsed list and reports a partial outcome near the selection (#652)", async () => {
    const local = { type: "file", name: "", state: "protected", cleanup_eligible: true, local_exists: true, local_size: 10 };
    const items = Array.from({ length: 100 }, (_, index) => ({ ...local, name: `f${index}.jpg`, path: `2026/f${index}.jpg` }));
    const posted: string[] = [];
    configureApiClient({ fetch: vi.fn(async (input, init) => {
      const url = String(input);
      if (url.startsWith("/api/free-space")) {
        const body = JSON.parse(String(init?.body)) as { path: string }; posted.push(body.path);
        return body.path === "2026/f3.jpg" ? jsonResponse({ detail: "Archive Version not verified" }, 409) : jsonResponse({ ok: true });
      }
      if (url.startsWith("/api/jobs")) return jsonResponse({ groups: [] });
      if (url.startsWith("/api/stats")) return jsonResponse({ states: {}, storage: {}, runtime: {}, source_volume: {} });
      return jsonResponse({ items, total: items.length, aggregate_status: "ready" });
    }) });
    window.history.replaceState({}, "", "/");
    render(<ApiQueryProvider client={createAppQueryClient()}><FileBrowser vaultId={1} vaultName="First" t={(key, params) => params ? `${key} ${JSON.stringify(params)}` : key} capabilities={{ delete_enabled: true, cloud_deletion_enabled: true }} /></ApiQueryProvider>);
    await screen.findAllByText("f0.jpg"); const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox", { name: "files.select_page" }));
    await user.click(within(screen.getByTestId("selection-bar")).getByRole("button", { name: "essential.free_now" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent('ui.confirm_free_space_description {"count":"100"}');
    expect(dialog).not.toHaveTextContent("2026/f0.jpg, ");
    const list = within(dialog).getByTestId("free-space-items");
    expect(list.tagName).toBe("DETAILS"); expect(list).not.toHaveAttribute("open");
    expect(within(list).getAllByRole("listitem", { hidden: true })).toHaveLength(100);
    // The paths live in the scrolling body; the actions stay outside it, always reachable.
    const body = within(dialog).getByTestId("confirm-dialog-body");
    expect(body).toHaveClass("overflow-y-auto", "min-h-0"); expect(body).toContainElement(list);
    expect(dialog).toHaveClass("max-h-[calc(100dvh-1.75rem)]", "flex-col");
    const confirm = within(dialog).getByRole("button", { name: "essential.free_now" });
    expect(body).not.toContainElement(confirm);
    await user.click(confirm);
    const outcome = await screen.findByTestId("free-space-outcome");
    expect(posted).toHaveLength(100);
    expect(outcome).toHaveTextContent('files.free_space_partial {"freed":"99","total":"100"}');
    expect(outcome).toHaveTextContent("First / 2026/f3.jpg: Archive Version not verified");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("free-space-outcome").compareDocumentPosition(screen.getByTestId("file-list-table")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
  it("labels the page as a readable range", async () => {
    setup(); await screen.findAllByText("photo.jpg");
    expect(screen.getByTestId("page-label")).toHaveTextContent("files.range");
  });
});
