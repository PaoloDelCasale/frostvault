import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import type { CloudDeletionPreview } from "@/api/types";
import { FileBrowser } from "@/pages/archive/FileBrowser";
import { CloudPurgeDialog } from "@/pages/archive/CloudPurgeDialog";
import { translate } from "@/i18n/translate";
import { jsonResponse } from "./vault-settings-harness";

const t = (key: string, params?: Record<string, string | number>) => params ? `${key} ${JSON.stringify(params)}` : key;
const preview = (overrides: Partial<CloudDeletionPreview> = {}): CloudDeletionPreview => ({
  object_count: 2149, version_count: 2149, delete_marker_count: 0, byte_count: 6_270_000_000,
  fingerprint: "a".repeat(64), mode: "selection", duplicates_only: true,
  eligible_count: 2163, eligible_bytes: 6_600_000_000, duplicate_count: 2149, duplicate_bytes: 6_270_000_000,
  no_local_copy_count: 14, no_local_copy_bytes: 330_000_000,
  skipped: { present_locally: 137496, pending_work: 0, pending_cleanup: 0, conflict: 0, not_verified: 0, no_exact_version: 0 },
  skipped_count: 137496, ...overrides,
});

const folder = { type: "directory", name: "library", path: "library", state: "protected", item_count: 139659, total_size: 1, state_counts: { protected: 137496, cloud_only: 2163 }, available_actions: { upload: 0, recover: 2163, "free-space": 0, "cloud-purge": 2163 } };
const present = { type: "file", name: "a.jpg", path: "a.jpg", state: "protected", local_exists: true, cloud_exists: true, cleanup_eligible: true };
const cloudOnly = { type: "file", name: "b.jpg", path: "b.jpg", state: "cloud_only", local_exists: false, cloud_exists: true, recover_eligible: true };

function setup(items: unknown[], total = items.length, search = "") {
  window.history.replaceState({}, "", `/${search}`);
  const posts: { url: string; body: Record<string, unknown> }[] = [];
  let purgeStatus = 202;
  configureApiClient({ fetch: vi.fn(async (input, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST") posts.push({ url, body: JSON.parse(String(init.body)) as Record<string, unknown> });
    if (url.startsWith("/api/cloud-deletion/preview")) {
      const body = posts.at(-1)!.body;
      return jsonResponse(preview(body.duplicates_only ? {} : { object_count: 2163, duplicates_only: false }));
    }
    if (url.startsWith("/api/cloud-deletion")) return jsonResponse({ purge_delay_seconds: 604800 });
    if (url.startsWith("/api/cloud-purge")) {
      if (purgeStatus === 409) { purgeStatus = 202; return jsonResponse({ detail: "changed", message: "changed", message_key: "api.cloud_purge_selection_changed" }, 409); }
      return jsonResponse({ group_id: "g", job_ids: [1], status: "pending_delay" }, 202);
    }
    if (url.startsWith("/api/stats")) return jsonResponse({ states: {}, storage: {}, active_jobs: 0, runtime: {}, source_volume: {} });
    if (url.startsWith("/api/jobs")) return jsonResponse({ groups: [] });
    return jsonResponse({ items, total, aggregate_status: "ready" });
  }) });
  render(<ApiQueryProvider client={createAppQueryClient()}><FileBrowser vaultId={1} vaultName="Immich" t={t} capabilities={{ delete_enabled: true, cloud_deletion_enabled: true }} /></ApiQueryProvider>);
  return { posts, fail409: () => { purgeStatus = 409; } };
}
afterEach(() => { resetApiClientForTests(); window.history.replaceState({}, "", "/"); });
const check = (path: string) => screen.getAllByRole("checkbox", { name: new RegExp(`essential.select Immich / ${path}$`) })[0]!;

describe("Delete from cloud on a selection (#723)", () => {
  it("offers it for folders and files mixed, previews with «only duplicates» on and confirms the fingerprint", async () => {
    const { posts } = setup([folder, present, cloudOnly]);
    await screen.findAllByText("library");
    const user = userEvent.setup();
    await user.click(check("a.jpg"));
    expect(within(screen.getByTestId("selection-bar")).queryByTestId("selection-cloud-purge")).not.toBeInTheDocument();
    await user.click(check("library"));
    await user.click(check("b.jpg"));
    await user.click(screen.getByTestId("selection-cloud-purge"));
    const skipped = await screen.findByTestId("cloud-purge-skipped");
    expect(skipped).toHaveTextContent('ui.cloud_purge_skip.present_locally {"count":137496}');
    expect(screen.getByTestId("cloud-purge-preview")).toHaveTextContent('ui.cloud_purge_duplicates {\\"count\\":2149}');
    expect(screen.getByTestId("cloud-purge-excluded")).toHaveTextContent('ui.cloud_purge_no_local_copy {"count":14}');
    expect(screen.getByTestId("cloud-purge-scope")).toHaveTextContent("a.jpg, library/, b.jpg");
    expect(screen.getByTestId("cloud-purge-duplicates-only")).toBeChecked();
    expect(posts.at(-1)).toEqual({ url: expect.stringContaining("/api/cloud-deletion/preview"), body: { paths: ["a.jpg", "b.jpg"], directories: ["library"], duplicates_only: true } });

    await user.click(screen.getByTestId("cloud-purge-duplicates-only"));
    await waitFor(() => expect(posts.at(-1)!.body.duplicates_only).toBe(false));
    await screen.findByTestId("cloud-purge-preview");
    await user.click(screen.getByTestId("cloud-purge-duplicates-only"));
    await waitFor(() => expect(posts.at(-1)!.body.duplicates_only).toBe(true));
    await screen.findByTestId("cloud-purge-preview");

    await user.type(screen.getByTestId("cloud-purge-reason"), "moved by Immich");
    await user.type(screen.getByTestId("cloud-purge-confirmation"), "Immich");
    await user.click(screen.getByTestId("cloud-purge-submit"));
    await waitFor(() => expect(posts.some(({ url }) => url.startsWith("/api/cloud-purge"))).toBe(true));
    expect(posts.find(({ url }) => url.startsWith("/api/cloud-purge"))!.body).toEqual({
      paths: ["a.jpg", "b.jpg"], directories: ["library"], duplicates_only: true,
      fingerprint: "a".repeat(64), reason: "moved by Immich", confirmation: "Immich",
    });
    await waitFor(() => expect(screen.queryByTestId("cloud-purge-form")).not.toBeInTheDocument());
  });

  it("selects every result of a filtered view beyond the page, for cloud deletion only", async () => {
    const { posts, fail409 } = setup([cloudOnly], 2163, "?state=cloud_only&directory=library");
    await screen.findAllByText("b.jpg");
    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox", { name: "files.select_page" }));
    await user.click(screen.getByTestId("select-all-results"));
    expect(screen.getByTestId("selection-bar")).toHaveTextContent("files.all_results_selected");
    expect(screen.getByTestId("all-results-note")).toBeInTheDocument();
    await user.click(screen.getByTestId("selection-cloud-purge"));
    await screen.findByTestId("cloud-purge-preview");
    expect(posts.at(-1)!.body).toEqual({ listing: { directory: "library", q: "", state: "cloud_only" }, duplicates_only: true });

    fail409();
    await user.type(screen.getByTestId("cloud-purge-reason"), "cleanup");
    await user.type(screen.getByTestId("cloud-purge-confirmation"), "Immich");
    await user.click(screen.getByTestId("cloud-purge-submit"));
    // A changed selection shows why and loads a new preview to confirm.
    expect(await screen.findByTestId("cloud-purge-error")).toHaveTextContent("api.cloud_purge_selection_changed");
    await waitFor(() => expect(posts.at(-1)!.url).toContain("/api/cloud-deletion/preview"));
    await screen.findByTestId("cloud-purge-preview");
  });

  it("does not offer «Select all» when the page holds every result", async () => {
    setup([cloudOnly], 1);
    await screen.findAllByText("b.jpg");
    await userEvent.setup().click(screen.getByRole("checkbox", { name: "files.select_page" }));
    expect(screen.queryByTestId("select-all-results")).not.toBeInTheDocument();
    expect(screen.getByTestId("selection-cloud-purge")).toBeInTheDocument();
  });
});

describe("CloudPurgeDialog selection preview", () => {
  it("keeps confirmation off when nothing is eligible", () => {
    render(<CloudPurgeDialog open onOpenChange={() => undefined} path="library/" vaultName="Immich" settings={null}
      preview={preview({ object_count: 0, version_count: 0, byte_count: 0, eligible_count: 14, duplicate_count: 0, no_local_copy_count: 14 })}
      t={t} duplicatesOnly onDuplicatesOnlyChange={() => undefined} onConfirm={() => undefined} />);
    expect(screen.getByTestId("cloud-purge-nothing")).toBeInTheDocument();
    expect(screen.queryByTestId("cloud-purge-preview")).not.toBeInTheDocument();
    expect(screen.getByTestId("cloud-purge-submit")).toBeDisabled();
  });
  it("shows the exact-file preview without selection counts or toggle", () => {
    render(<CloudPurgeDialog open onOpenChange={() => undefined} path="a.jpg" vaultName="Immich" settings={null}
      preview={preview({ mode: "exact", object_count: 1 })} t={t} onConfirm={() => undefined} />);
    expect(screen.getByTestId("cloud-purge-preview")).toBeInTheDocument();
    expect(screen.queryByTestId("cloud-purge-skipped")).not.toBeInTheDocument();
    expect(screen.queryByTestId("cloud-purge-duplicates-only")).not.toBeInTheDocument();
  });
});

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const catalog = (locale: string): Record<string, string> => JSON.parse(readFileSync(path.join(root, `app/locales/${locale}.json`), "utf8"));

describe("«Delete from cloud?» summary in Italian and English (#725)", () => {
  const skippedAll = { present_locally: 1860, pending_work: 1, pending_cleanup: 2, conflict: 1, not_verified: 3, no_exact_version: 1 };
  const cases = [
    { locale: "it", expected: ["Selezione: cartella upload/", "Da eliminare", "23 doppioni", "Esclusi", "14 solo nel cloud", "Saltati", "1860 presenti sul server".replace("1860", new Intl.NumberFormat("it").format(1860))] },
    { locale: "en", expected: ["Selection: folder upload/", "To delete", "23 Cloud Duplicates", "Excluded", "14 only in the cloud", "Skipped", "1,860 present on the server"] },
  ];
  for (const { locale, expected } of cases) {
    it(`shows translated plural counts in ${locale}, never raw keys`, () => {
      const messages = catalog(locale);
      const tr = (key: string, params?: Record<string, string | number>) => translate(messages, key, params, locale);
      const scope = tr("ui.cloud_purge_scope_folder", { path: "upload/" });
      const { unmount } = render(<CloudPurgeDialog open onOpenChange={() => undefined} path={scope} vaultName="Immich" settings={{ purge_delay_seconds: 604800 } as never}
        preview={preview({ object_count: 23, duplicate_count: 23, eligible_count: 37, no_local_copy_count: 14, skipped: skippedAll })}
        t={tr} duplicatesOnly onDuplicatesOnlyChange={() => undefined} onConfirm={() => undefined} />);
      const text = document.body.textContent ?? "";
      expect(text).not.toMatch(/ui\./);
      for (const fragment of expected) expect(text).toContain(fragment);
      expect(text).not.toMatch(/marker/i);
      unmount();
      // «Only Cloud Duplicates» off: every eligible file, split into duplicates and only-in-cloud.
      render(<CloudPurgeDialog open onOpenChange={() => undefined} path={scope} vaultName="Immich" settings={null}
        preview={preview({ duplicates_only: false, object_count: 1, duplicate_count: 1, eligible_count: 2, no_local_copy_count: 1, skipped: {} })}
        t={tr} duplicatesOnly={false} onDuplicatesOnlyChange={() => undefined} onConfirm={() => undefined} />);
      expect(document.body.textContent).not.toMatch(/ui\./);
      expect(screen.getByTestId("cloud-purge-split")).toBeInTheDocument();
      expect(screen.queryByTestId("cloud-purge-excluded")).not.toBeInTheDocument();
    });
  }
});
