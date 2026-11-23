import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { configureApiClient, resetApiClientForTests } from "@/api";
import { FileBrowser } from "@/pages/archive/FileBrowser";
import { createVaultAccessFetch, defaultPolicy, renderVaultAccess } from "./vault-settings-harness";
import { jsonResponse } from "./support/http";
import { renderWithQueryClient } from "./support/queryClient";

type Call = { url: string; method: string; body: unknown };
const library = { type: "directory", name: "library", path: "library", state_counts: { protected: 2 }, total_size: 10 };
const photo = { type: "file", name: "a.jpg", path: "library/a.jpg", state: "protected", local_exists: true, local_size: 10 };
const policy = { ...defaultPolicy, exclude_globs: ["upload/**", "**/cache/**"], fixed_exclude_globs: ["._*", ".DS_Store"] };

function setup(search: string, routes: { items?: unknown[]; disk?: unknown; preview?: unknown } = {}) {
  window.history.replaceState({}, "", `/${search}`);
  const calls: Call[] = [];
  configureApiClient({ fetch: vi.fn(async (input, init) => {
    const url = String(input); const method = init?.method ?? "GET";
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (url.startsWith("/api/vaults/3/excluded-folders")) return jsonResponse(routes.disk ?? { directory: "", excluded_by: null, folders: [], entries: [], total_entries: 0, truncated: false });
    if (url.startsWith("/api/vaults/3/operation-policy")) return jsonResponse(policy);
    if (url.startsWith("/api/vaults/3/exclusions/inclusion-preview")) return jsonResponse(routes.preview);
    if (url.startsWith("/api/vaults/3/exclusions/")) return jsonResponse(policy);
    if (/^\/api\/vaults\/\d+\/stats$/.test(url)) return jsonResponse({ states: {}, protection_state: "protected", storage: {}, active_jobs: 0, runtime: {}, source_volume: {} });
    if (/^\/api\/vaults\/\d+\/jobs/.test(url)) return jsonResponse({ groups: [] });
    const items = routes.items ?? [];
    return jsonResponse({ items, total: items.length, aggregate_status: "ready" });
  }) });
  renderWithQueryClient(<FileBrowser vaultId={3} vaultName="Immich" t={(key, params) => params ? `${key} ${JSON.stringify(params)}` : key} capabilities={{ delete_enabled: true, cloud_deletion_enabled: true }} />);
  return calls;
}
afterEach(() => { resetApiClientForTests(); window.history.replaceState({}, "", "/"); });
const table = () => screen.getByTestId("file-list-table");

describe("exclusions in the Vault browser (#754)", () => {
  it("shows an Excluded Folder without catalog rows in its place, with its rule", async () => {
    const calls = setup("", { items: [library], disk: { directory: "", excluded_by: null, folders: [{ name: "upload", path: "upload", rule: "upload/**" }], entries: [], total_entries: 0, truncated: false } });
    const row = await waitFor(() => { const found = table().querySelector("[data-excluded-folder='upload']"); expect(found).not.toBeNull(); return found as HTMLElement; });
    expect(within(row).getByText("exclusions.excluded_folder")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: /upload\/\*\*/ })).toHaveAttribute("href", "/vaults/3/settings#exclusions");
    expect(within(table()).getAllByText("library").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("file-list-empty")).not.toBeInTheDocument();
    expect(calls.find((call) => call.url.startsWith("/api/vaults/3/excluded-folders"))).toBeDefined();
  });

  it("lists an Excluded Folder from disk, each entry unprotected, folders navigable", async () => {
    const calls = setup("?directory=upload", { disk: { directory: "upload", excluded_by: "upload/**", folders: [], total_entries: 2, truncated: false, entries: [
      { name: "u1", path: "upload/u1", type: "directory", size: null, modified_at: "2026-11-17T10:00:00+00:00" },
      { name: "c.jpg", path: "upload/c.jpg", type: "file", size: 30, modified_at: "2026-11-17T10:00:00+00:00" },
    ] } });
    expect(await screen.findByTestId("excluded-folder-banner")).toHaveTextContent("upload/**");
    expect(within(table()).getAllByTestId("disk-entry-label")).toHaveLength(2);
    expect(screen.queryByTestId("file-list-empty")).not.toBeInTheDocument();
    await userEvent.setup().click(within(table()).getByRole("button", { name: "u1" }));
    await waitFor(() => expect(calls.some((call) => call.url.startsWith("/api/vaults/3/excluded-folders?directory=upload%2Fu1"))).toBe(true));
  });

  it("excludes a file by its exact path after a confirmation that the cloud copies stay", async () => {
    const calls = setup("?directory=library", { items: [photo] });
    const user = userEvent.setup();
    await user.click(await within(await screen.findByTestId("file-list-table")).findByRole("button", { name: /exclusions.actions/ }));
    await user.click(await screen.findByRole("menuitem", { name: "exclusions.exclude" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("exclusions.confirm_exclude_file");
    expect(dialog).toHaveTextContent("library/a.jpg");
    await user.click(within(dialog).getByRole("button", { name: "exclusions.exclude" }));
    await waitFor(() => expect(calls.find((call) => call.method === "POST")).toMatchObject({ body: { path: "library/a.jpg", is_directory: false } }));
    expect(calls.find((call) => call.method === "POST")?.url).toBe("/api/vaults/3/exclusions/exclude");
  });

  it("disables «Exclude» for a name with glob characters and points to the panel", async () => {
    setup("?directory=library", { items: [{ ...photo, name: "a[1].jpg", path: "library/a[1].jpg" }] });
    const user = userEvent.setup();
    await user.click(await within(await screen.findByTestId("file-list-table")).findByRole("button", { name: /exclusions.actions/ }));
    expect(await screen.findByRole("menuitem", { name: "exclusions.exclude" })).toHaveAttribute("data-disabled");
    expect(screen.getByTestId("exclude-glob-hint")).toHaveTextContent("exclusions.exclude_glob_disabled");
    expect(screen.getByRole("menuitem", { name: "exclusions.open_panel" })).toBeInTheDocument();
  });

  it("disables «Exclude» for a file at the Vault root: its rule would match the name everywhere", async () => {
    setup("", { items: [{ ...photo, name: "notes.txt", path: "notes.txt" }] });
    const user = userEvent.setup();
    await user.click(await within(await screen.findByTestId("file-list-table")).findByRole("button", { name: /exclusions.actions/ }));
    expect(await screen.findByRole("menuitem", { name: "exclusions.exclude" })).toHaveAttribute("data-disabled");
    expect(screen.getByTestId("exclude-glob-hint")).toHaveTextContent("exclusions.exclude_root_file_disabled");
  });

  it("includes a folder again with what will be uploaded, and the scan it starts", async () => {
    const calls = setup("", { disk: { directory: "", excluded_by: null, folders: [{ name: "upload", path: "upload", rule: "upload/**" }], entries: [], total_entries: 0, truncated: false },
      preview: { path: "upload", is_directory: true, own_rule: "upload/**", own_rule_present: true, excluded: true, rules: [], fixed: [], not_included: false, includable: true, folder_rule: true, files: 1200, bytes: 5e9, complete: false, estimate: { estimated_cost_eur: 0.11 } } });
    const user = userEvent.setup();
    const row = await waitFor(() => { const found = table().querySelector("[data-excluded-folder='upload']"); expect(found).not.toBeNull(); return found as HTMLElement; });
    await user.click(within(row).getByRole("button", { name: /exclusions.actions/ }));
    await user.click(await screen.findByRole("menuitem", { name: "exclusions.include" }));
    const preview = await screen.findByTestId("inclusion-preview");
    expect(preview).toHaveTextContent("exclusions.include_upload_minimum");
    expect(preview).toHaveTextContent("1200");
    expect(preview).toHaveTextContent("exclusions.remove_folder_scan");
    expect(preview).toHaveTextContent("exclusions.system_folders");
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "exclusions.include" }));
    await waitFor(() => expect(calls.find((call) => call.method === "POST")).toMatchObject({ body: { path: "upload", is_directory: true } }));
    expect(calls.find((call) => call.method === "POST")?.url).toContain("/api/vaults/3/exclusions/include");
  });

  it("names a broader rule instead of removing it", async () => {
    const calls = setup("", { disk: { directory: "", excluded_by: null, folders: [{ name: "cache", path: "cache", rule: "**/cache/**" }], entries: [], total_entries: 0, truncated: false },
      preview: { path: "cache", is_directory: true, own_rule: "cache/**", own_rule_present: false, excluded: true, rules: ["**/cache/**"], fixed: [], not_included: false, includable: false, folder_rule: false, files: null, bytes: null, complete: null, estimate: null } });
    const user = userEvent.setup();
    const row = await waitFor(() => { const found = table().querySelector("[data-excluded-folder='cache']"); expect(found).not.toBeNull(); return found as HTMLElement; });
    await user.click(within(row).getByRole("button", { name: /exclusions.actions/ }));
    await user.click(await screen.findByRole("menuitem", { name: "exclusions.include" }));
    const dialog = await screen.findByRole("alertdialog");
    await waitFor(() => expect(dialog).toHaveTextContent("exclusions.broader"));
    expect(dialog).toHaveTextContent("**/cache/**");
    expect(within(dialog).getByRole("button", { name: "exclusions.open_panel" })).toBeInTheDocument();
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });
});

describe("Exclusions panel in the Vault settings (#754)", () => {
  it("lists editable rules and the fixed exclusions, and edits one rule at a time", async () => {
    let stored = { ...defaultPolicy, exclude_globs: ["upload/**"], include_globs: [] as string[], fixed_exclude_globs: ["._*", ".DS_Store"] };
    const edits: unknown[] = [];
    const fetchImpl = createVaultAccessFetch({
      "GET /api/vaults/3/operation-policy": () => jsonResponse(stored),
      "POST /api/vaults/3/operation-policy/globs": (init) => {
        const edit = JSON.parse(String(init?.body)) as { field: "exclude_globs" | "include_globs"; action: string; glob: string };
        edits.push(edit);
        const list = stored[edit.field];
        stored = { ...stored, [edit.field]: edit.action === "add" ? [...list, edit.glob] : list.filter((rule) => rule !== edit.glob) };
        return jsonResponse(stored);
      },
      "POST /api/vaults/3/operation-policy/preview-globs": () => jsonResponse({ included: ["library/a.jpg"], excluded: ["upload/b.jpg"] }),
    }, { locale: "it" });
    renderVaultAccess({ vaultId: 3, fetchImpl });
    const panel = await screen.findByTestId("exclusions-panel");
    expect(await within(panel).findByText("upload/**")).toBeInTheDocument();
    expect(within(within(panel).getByTestId("fixed-rules")).getByText(".DS_Store")).toBeInTheDocument();
    expect(within(panel).getByTestId("exclusions-order-note")).toHaveTextContent("Le esclusioni si applicano dopo le inclusioni");
    const user = userEvent.setup();
    await user.type(within(within(panel).getByTestId("exclude-rules")).getByRole("textbox"), "thumbs/**");
    await user.click(within(within(panel).getByTestId("exclude-rules")).getByRole("button", { name: "Aggiungi" }));
    expect(await within(panel).findByText("thumbs/**")).toBeInTheDocument();
    await user.type(within(within(panel).getByTestId("include-rules")).getByRole("textbox"), "**/*.jpg");
    await user.click(within(within(panel).getByTestId("include-rules")).getByRole("button", { name: "Aggiungi" }));
    await user.click(within(panel).getByRole("button", { name: "Togli upload/**" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("avvia una scansione completa");
    await user.click(within(dialog).getByRole("button", { name: "Togli" }));
    await waitFor(() => expect(within(panel).queryByText("upload/**")).not.toBeInTheDocument());
    expect(edits).toEqual([
      { field: "exclude_globs", action: "add", glob: "thumbs/**" },
      { field: "include_globs", action: "add", glob: "**/*.jpg" },
      { field: "exclude_globs", action: "remove", glob: "upload/**" },
    ]);
    expect(fetchImpl.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
    await user.click(within(panel).getByText("Prova le regole"));
    await user.type(within(panel).getByRole("textbox", { name: "Percorsi da provare, uno per riga" }), "library/a.jpg{enter}upload/b.jpg");
    await user.click(within(panel).getByRole("button", { name: "Anteprima" }));
    expect(await within(panel).findByTestId("glob-preview")).toHaveTextContent("upload/b.jpg");
  });
});
