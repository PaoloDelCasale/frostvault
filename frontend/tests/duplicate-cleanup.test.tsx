import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";
import { problemLink } from "@/lib/problemLink";
import { vaultFilesUrl, vaultSettingsUrl } from "@/router";
import { createVaultAccessFetch, defaultDuplicateCleanup, jsonResponse, loadCatalog, renderVaultAccess } from "./vault-settings-harness";

afterEach(() => { cleanup(); resetApiClientForTests(); });

const URL = "/api/vaults/1/duplicate-cleanup";

// #726: Automatic Duplicate Cleanup is a per-Vault setting, off by default.
describe("Automatic Duplicate Cleanup settings", () => {
  it("is off by default and turns on with one click, confirming «Saved» beside it", async () => {
    const user = userEvent.setup();
    const updates: unknown[] = [];
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`PUT ${URL}`]: (init) => {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        updates.push(body);
        return jsonResponse({ ...defaultDuplicateCleanup, ...body });
      },
    }) });
    const section = (await screen.findByRole("heading", { name: "Cloud duplicates", level: 2 })).closest("section")!;
    expect(section).toHaveAttribute("id", "duplicates");
    const toggle = await within(section).findByRole("checkbox", { name: "Automatically delete duplicates from the cloud" });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).not.toBeChecked();
    expect(within(section).getByText(/Off: duplicates stay in the cloud/)).toBeInTheDocument();
    expect(within(section).queryByRole("button", { name: "Save wait and caps" })).toBeNull();

    await user.click(toggle);
    await waitFor(() => expect(updates).toEqual([{ enabled: true }]));
    expect(toggle).toBeChecked();
    expect(await within(section).findByTestId("saved-indicator")).toHaveTextContent("Saved");
    expect(within(section).getByLabelText("Wait before a file counts as a duplicate (days)")).toHaveValue("7");
    expect(within(section).getByLabelText("File cap per run")).toHaveValue("5,000");
    expect(within(section).getByLabelText("Size cap per run (GiB)")).toHaveValue("50");
    expect(within(section).getByTestId("duplicate-cleanup-last-run")).toHaveTextContent("Not run yet.");
  });

  it("saves the wait and caps only once they change and are valid", async () => {
    const user = userEvent.setup();
    const updates: unknown[] = [];
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`GET ${URL}`]: () => jsonResponse({ ...defaultDuplicateCleanup, enabled: true }),
      [`PUT ${URL}`]: (init) => {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        updates.push(body);
        return jsonResponse({ ...defaultDuplicateCleanup, enabled: true, ...body });
      },
    }) });
    const save = await screen.findByRole("button", { name: "Save wait and caps" });
    expect(save).toBeDisabled();
    const grace = screen.getByLabelText("Wait before a file counts as a duplicate (days)");
    await user.clear(grace);
    await user.type(grace, "91");
    expect(save).toBeDisabled();
    await user.clear(grace);
    await user.type(grace, "14");
    const gib = screen.getByLabelText("Size cap per run (GiB)");
    await user.clear(gib);
    await user.type(gib, "2");
    expect(save).toBeEnabled();
    await user.click(save);
    await waitFor(() => expect(updates).toEqual([{ grace_days: 14, cap_files: 5000, cap_bytes: 2 * 1024 ** 3 }]));
    expect(await screen.findByText("Duplicate settings saved.")).toBeInTheDocument();
    await waitFor(() => expect(save).toBeDisabled());
  });

  it("shows the last run and a pending automatic deletion that can be cancelled", async () => {
    const user = userEvent.setup();
    const cancels: unknown[] = [];
    let pending = [{ group_id: "g-1", file_count: 2149, byte_count: 6.6 * 1024 ** 3, run_after: "2026-11-23T03:00:00Z", cancellable: true }];
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`GET ${URL}`]: () => jsonResponse({
        ...defaultDuplicateCleanup, enabled: true, last_run_at: "2026-11-16T03:00:00Z", next_run_at: "2026-11-17T03:00:00Z",
        last_outcome: { outcome: "scheduled", at: "2026-11-16T03:00:00Z", file_count: 2149, byte_count: 6.6 * 1024 ** 3 },
        pending,
      }),
      "POST /api/jobs/cancel": (init) => {
        cancels.push(JSON.parse(String(init?.body)));
        pending = [];
        return jsonResponse({ cancelled: 2149 });
      },
    }) });
    expect(await screen.findByTestId("duplicate-cleanup-last-run")).toHaveTextContent(/Last run: .*2,149 duplicates scheduled \(6\.6 GiB\)/);
    const card = screen.getByTestId("duplicate-cleanup-pending");
    expect(card).toHaveTextContent(/Deletion of 2,149 duplicates \(6\.6 GiB\) scheduled/);
    await user.click(within(card).getByRole("button", { name: "Cancel this deletion" }));
    await waitFor(() => expect(cancels).toEqual([{ group_id: "g-1", action: "cloud-purge" }]));
    await waitFor(() => expect(screen.queryByTestId("duplicate-cleanup-pending")).toBeNull());
    expect(await screen.findByText("Automatic deletion cancelled.")).toBeInTheDocument();
  });

  // #729: the duplicates now, those still waiting and the totals, above the last run.
  it("shows the duplicates now, those waiting, those ready and the totals", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`GET ${URL}`]: () => jsonResponse({
        ...defaultDuplicateCleanup, enabled: true, last_run_at: "2026-11-16T23:46:00Z", next_run_at: "2026-11-17T23:46:00Z",
        last_outcome: { outcome: "none", at: "2026-11-16T23:46:00Z", file_count: 0, byte_count: 0, waiting_count: 2149, waiting_bytes: 6.3 * 1024 ** 3 },
        current: { count: 2152, bytes: 6.4 * 1024 ** 3 },
        waiting: { count: 2149, bytes: 6.3 * 1024 ** 3, first_eligible_at: "2026-11-23T10:00:00Z", earliest_deletion_at: "2026-11-30T23:46:00Z" },
        ready: { count: 3, bytes: 3 * 1024 ** 2 },
        totals: { deleted_count: 1200, deleted_bytes: 2 * 1024 ** 3, last_deleted_at: "2026-11-10T03:00:00Z" },
      }),
    }) });
    expect(await screen.findByTestId("duplicate-cleanup-current")).toHaveTextContent("Cloud duplicates now: 2,152 (6.4 GiB).");
    expect(screen.getByTestId("duplicate-cleanup-waiting")).toHaveTextContent(
      /^2,149 duplicates \(6\.3 GiB\) waiting: eligible from Nov 23, 2026, deletion not before (Nov 30|Dec 1), 2026\.$/);
    expect(screen.getByTestId("duplicate-cleanup-ready")).toHaveTextContent("3 duplicates (3.0 MiB) ready for the next run.");
    expect(screen.getByTestId("duplicate-cleanup-totals")).toHaveTextContent(
      "Deleted so far: 1,200 duplicates, 2.0 GiB freed; last deletion on Nov 10, 2026.");
    const lastRun = screen.getByTestId("duplicate-cleanup-last-run");
    expect(lastRun).toHaveTextContent(/no duplicate has finished its wait yet \(2,149 waiting\)/);
    expect(lastRun).not.toHaveTextContent("no duplicates to delete");
    // The counts come before the last run.
    expect(screen.getByTestId("duplicate-cleanup-counts").compareDocumentPosition(lastRun) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("says «no duplicates to delete» only when there are none, also for older outcomes", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`GET ${URL}`]: () => jsonResponse({
        ...defaultDuplicateCleanup, enabled: true, last_run_at: "2026-11-16T23:46:00Z", next_run_at: "2026-11-17T23:46:00Z",
        last_outcome: { outcome: "none", at: "2026-11-16T23:46:00Z", file_count: 0, byte_count: 0 },
      }),
    }) });
    expect(await screen.findByTestId("duplicate-cleanup-last-run")).toHaveTextContent(/no duplicates to delete/);
    expect(screen.getByTestId("duplicate-cleanup-current")).toHaveTextContent("No cloud duplicates right now.");
    expect(screen.queryByTestId("duplicate-cleanup-waiting")).toBeNull();
    expect(screen.queryByTestId("duplicate-cleanup-ready")).toBeNull();
    expect(screen.queryByTestId("duplicate-cleanup-totals")).toBeNull();
  });

  it("shows the duplicates now and the totals while off, without waits or dates", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`GET ${URL}`]: () => jsonResponse({
        ...defaultDuplicateCleanup,
        current: { count: 4, bytes: 4096 },
        waiting: { count: 4, bytes: 4096, first_eligible_at: "2026-11-23T10:00:00Z", earliest_deletion_at: null },
        ready: { count: 0, bytes: 0 },
        totals: { deleted_count: 1, deleted_bytes: 1024, last_deleted_at: "2026-11-10T03:00:00Z" },
      }),
    }) });
    expect(await screen.findByTestId("duplicate-cleanup-current")).toHaveTextContent("Cloud duplicates now: 4 (4.0 KiB).");
    expect(screen.getByTestId("duplicate-cleanup-totals")).toHaveTextContent("Deleted so far: 1 duplicate, 1.0 KiB freed");
    expect(screen.queryByTestId("duplicate-cleanup-waiting")).toBeNull();
    expect(screen.queryByTestId("duplicate-cleanup-ready")).toBeNull();
    expect(screen.queryByTestId("duplicate-cleanup-last-run")).toBeNull();
  });

  it("says when the duplicates cannot be counted now", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`GET ${URL}`]: () => jsonResponse({ ...defaultDuplicateCleanup, current: null, waiting: null, ready: null, unseen: null }),
    }) });
    expect(await screen.findByTestId("duplicate-cleanup-current")).toHaveTextContent(/cannot be counted right now/);
    expect(screen.queryByTestId("duplicate-cleanup-unseen")).toBeNull();
  });

  // #729 review: duplicates never seen on the server are counted apart and never deleted automatically.
  it("counts the duplicates never seen on the server apart and points to «Delete from cloud»", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`GET ${URL}`]: () => jsonResponse({
        ...defaultDuplicateCleanup, enabled: true, last_run_at: "2026-11-16T23:46:00Z", next_run_at: "2026-11-17T23:46:00Z",
        last_outcome: { outcome: "none", at: "2026-11-16T23:46:00Z", file_count: 0, byte_count: 0, waiting_count: 0, waiting_bytes: 0, unseen_count: 2 },
        current: { count: 2, bytes: 2048 },
        unseen: { count: 2, bytes: 2048 },
      }),
    }) });
    expect(await screen.findByTestId("duplicate-cleanup-current")).toHaveTextContent("Cloud duplicates now: 2 (2.0 KiB).");
    expect(screen.getByTestId("duplicate-cleanup-unseen")).toHaveTextContent(
      "2 duplicates (2.0 KiB) never seen on the server: the automatic cleanup does not delete them. You can delete them yourself with «Delete from cloud» and «Only Cloud Duplicates».");
    expect(screen.queryByTestId("duplicate-cleanup-waiting")).toBeNull();
    expect(screen.queryByTestId("duplicate-cleanup-ready")).toBeNull();
    const lastRun = screen.getByTestId("duplicate-cleanup-last-run");
    expect(lastRun).toHaveTextContent(/no duplicate the cleanup can delete \(2 never seen on the server\)/);
    expect(lastRun).not.toHaveTextContent("no duplicates to delete");
  });

  it("shows the duplicates never seen on the server while off too, in Italian", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`GET ${URL}`]: () => jsonResponse({ ...defaultDuplicateCleanup, current: { count: 1, bytes: 1024 }, unseen: { count: 1, bytes: 1024 } }),
    }, { locale: "it" }) });
    expect(await screen.findByTestId("duplicate-cleanup-unseen")).toHaveTextContent(
      "1 doppione (1,0 KiB) mai visto sul server: la pulizia automatica non lo elimina. Puoi eliminarlo tu con «Elimina dal cloud» e «Solo doppioni».");
  });

  it("writes and reads the caps with the Italian digit grouping", async () => {
    const user = userEvent.setup();
    const updates: unknown[] = [];
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`GET ${URL}`]: () => jsonResponse({ ...defaultDuplicateCleanup, enabled: true, cap_bytes: 1536 * 1024 ** 3, current: { count: 2149, bytes: 1024 } }),
      [`PUT ${URL}`]: (init) => {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        updates.push(body);
        return jsonResponse({ ...defaultDuplicateCleanup, enabled: true, ...body });
      },
    }, { locale: "it" }) });
    const files = await screen.findByLabelText("Soglia di file per esecuzione");
    await waitFor(() => expect(files).toHaveValue("5.000"));
    expect(screen.getByLabelText("Soglia di dimensione per esecuzione (GiB)")).toHaveValue("1.536");
    expect(screen.getByTestId("duplicate-cleanup-current")).toHaveTextContent("Doppioni nel cloud adesso: 2.149 (1,0 KiB).");
    const save = screen.getByRole("button", { name: "Salva attesa e soglie" });
    await user.clear(files);
    await user.type(files, "5000");
    expect(save).toBeDisabled();
    await user.clear(files);
    await user.type(files, "12.500");
    const gib = screen.getByLabelText("Soglia di dimensione per esecuzione (GiB)");
    await user.clear(gib);
    await user.type(gib, "0,5");
    expect(save).toBeEnabled();
    await user.click(save);
    await waitFor(() => expect(updates).toEqual([{ grace_days: 7, cap_files: 12500, cap_bytes: 0.5 * 1024 ** 3 }]));
    expect(await screen.findByLabelText("Soglia di file per esecuzione")).toHaveValue("12.500");
  });

  it("is hidden once the Vault's protection has stopped", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/general/vaults": () => jsonResponse({ items: [{ id: 1, name: "Test Archive", lifecycle: "tombstone", paused: false, source_root: "/sources/x" }] }),
    }) });
    await screen.findByRole("heading", { name: "Protection", level: 2 });
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Cloud duplicates", level: 2 })).toBeNull());
  });

  it("names every message in both languages", () => {
    for (const locale of ["en", "it"] as const) {
      const messages = loadCatalog(locale);
      for (const key of ["title", "summary", "enable", "off_hint", "how", "never_only_cloud", "save", "pending_one", "pending_other",
        "current_none", "current_one", "current_other", "current_unknown", "waiting_one", "waiting_other", "ready_one", "ready_other",
        "totals_one", "totals_other", "outcome.none_waiting_one", "outcome.none_waiting_other",
        "unseen_one", "unseen_other", "outcome.none_unseen_one", "outcome.none_unseen_other"]) {
        expect(messages[`duplicate_cleanup.${key}`], `${locale} ${key}`).toBeTruthy();
      }
    }
  });

  it("links a scheduled notice to the settings and a capped Problem to the Cloud-only files", () => {
    expect(problemLink(3, { duplicate_cleanup: true, group_id: "g-1" })).toBe(vaultSettingsUrl(3, "duplicates"));
    expect(problemLink(3, { duplicate_cleanup: true, count: 9000 })).toBe(vaultFilesUrl(3, { state: "cloud_only" }));
  });
});
