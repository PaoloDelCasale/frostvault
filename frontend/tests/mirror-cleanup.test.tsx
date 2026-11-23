import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";
import { problemLink } from "@/lib/problemLink";
import { vaultFilesUrl, vaultSettingsUrl } from "@/router";
import { createVaultAccessFetch, defaultMirrorCleanup, loadCatalog, renderVaultAccess } from "./vault-settings-harness";
import { jsonResponse } from "./support/http";

afterEach(() => { cleanup(); resetApiClientForTests(); });

const URL = "/api/vaults/1/mirror-cleanup";
const TITLE = "Delete from the cloud the files gone from the server";

async function mirrorSection() {
  return (await screen.findByRole("heading", { name: TITLE, level: 2 })).closest("section")!;
}

// #771: Mirror Cleanup is a per-Vault setting, off by default, beside Duplicate Cleanup.
describe("Mirror Cleanup settings", () => {
  it("is off by default, warns it is no protection, and turns on with one click", async () => {
    const user = userEvent.setup();
    const updates: unknown[] = [];
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`PUT ${URL}`]: (init) => {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        updates.push(body);
        return jsonResponse({ ...defaultMirrorCleanup, ...body });
      },
    }) });
    const section = await mirrorSection();
    expect(section).toHaveAttribute("id", "mirror");
    expect(within(section).getByTestId("mirror-cleanup-warning")).toHaveTextContent(
      "The cloud will no longer keep the files deleted from the server: it does not protect against accidental deletions.");
    const toggle = await within(section).findByRole("checkbox", { name: TITLE });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).not.toBeChecked();
    expect(within(section).getByText(/Off: files deleted from the server stay in the cloud/)).toBeInTheDocument();
    expect(within(section).getByTestId("mirror-cleanup-scope")).toHaveTextContent("Scope: the whole Vault.");
    expect(within(section).queryByRole("button", { name: "Save settings" })).toBeNull();

    await user.click(toggle);
    await waitFor(() => expect(updates).toEqual([{ enabled: true }]));
    expect(toggle).toBeChecked();
    expect(within(section).getByLabelText("Days since it left the server")).toHaveValue("7");
    expect(within(section).getByLabelText("File cap per run")).toHaveValue("5,000");
    expect(within(section).getByLabelText("Size cap per run (GiB)")).toHaveValue("50");
    expect(within(section).getByTestId("mirror-cleanup-last-run")).toHaveTextContent("Not run yet.");
  });

  it("saves the wait, caps and folders only once they change and are valid", async () => {
    const user = userEvent.setup();
    const updates: unknown[] = [];
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`GET ${URL}`]: () => jsonResponse({ ...defaultMirrorCleanup, enabled: true }),
      [`PUT ${URL}`]: (init) => {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        updates.push(body);
        return jsonResponse({ ...defaultMirrorCleanup, enabled: true, ...body });
      },
    }) });
    const section = await mirrorSection();
    const save = await within(section).findByRole("button", { name: "Save settings" });
    expect(save).toBeDisabled();
    const grace = within(section).getByLabelText("Days since it left the server");
    await user.clear(grace);
    await user.type(grace, "181");
    expect(save).toBeDisabled();
    await user.clear(grace);
    await user.type(grace, "30");
    await user.type(within(section).getByTestId("mirror-cleanup-folders"), "backup/daily\n\n  backup/weekly ");
    expect(save).toBeEnabled();
    await user.click(save);
    await waitFor(() => expect(updates).toEqual([{
      grace_days: 30, cap_files: 5000, cap_bytes: 50 * 1024 ** 3, folders: ["backup/daily", "backup/weekly"],
    }]));
    expect(await screen.findByText("Settings saved.")).toBeInTheDocument();
    expect(within(section).getByTestId("mirror-cleanup-scope")).toHaveTextContent("Scope: backup/daily, backup/weekly.");
    await waitFor(() => expect(save).toBeDisabled());
  });

  it("estimates the cloud life against the Minimum Storage Duration", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`GET ${URL}`]: () => jsonResponse({ ...defaultMirrorCleanup, enabled: true,
        cloud_life: { days_after_disappearance: 14, storage_class: "GLACIER_IR", class_from_day: 0, minimum_days: 90, minimum_until_day: 90, beyond_minimum: false } }),
    }) });
    const life = await screen.findByTestId("mirror-cleanup-cloud-life");
    expect(life).toHaveTextContent("plus 14 days after it disappears (7 of wait and 7 of cancellable delay)");
    expect(life).toHaveTextContent("the file is in GLACIER_IR at that age, which bills at least 90 days anyway: deleting it before day 90 saves nothing.");
  });

  it("has no minimum note when the plan keeps the file in Standard", async () => {
    renderVaultAccess();
    const life = await screen.findByTestId("mirror-cleanup-cloud-life");
    expect(life).toHaveTextContent("plus 14 days");
    expect(life).not.toHaveTextContent("bills at least");
  });

  it("shows the last run and a pending deletion that can be cancelled", async () => {
    const user = userEvent.setup();
    const cancels: unknown[] = [];
    let pending = [{ group_id: "m-1", file_count: 12, byte_count: 3 * 1024 ** 3, run_after: "2026-11-24T03:00:00Z", cancellable: true }];
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      [`GET ${URL}`]: () => jsonResponse({
        ...defaultMirrorCleanup, enabled: true, last_run_at: "2026-11-17T03:00:00Z", next_run_at: "2026-11-18T03:00:00Z",
        last_outcome: { outcome: "scheduled", at: "2026-11-17T03:00:00Z", file_count: 12, byte_count: 3 * 1024 ** 3 },
        current: { count: 12, bytes: 3 * 1024 ** 3 },
        pending,
      }),
      "POST /api/vaults/1/jobs/cancel": (init) => {
        cancels.push(JSON.parse(String(init?.body)));
        pending = [];
        return jsonResponse({ cancelled: 12 });
      },
    }) });
    expect(await screen.findByTestId("mirror-cleanup-last-run")).toHaveTextContent(/Last run: .*12 files scheduled \(3\.0 GiB\)/);
    expect(screen.getByTestId("mirror-cleanup-current")).toHaveTextContent("Cloud-only files in scope now: 12 (3.0 GiB).");
    const card = screen.getByTestId("mirror-cleanup-pending");
    expect(card).toHaveTextContent(/Deletion of 12 files \(3\.0 GiB\) scheduled/);
    await user.click(within(card).getByRole("button", { name: "Cancel this deletion" }));
    await waitFor(() => expect(cancels).toEqual([{ group_id: "m-1", action: "cloud-purge" }]));
    await waitFor(() => expect(screen.queryByTestId("mirror-cleanup-pending")).toBeNull());
  });

  it("is hidden once the Vault's protection has stopped", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/general/vaults": () => jsonResponse({ items: [{ id: 1, name: "Test Archive", lifecycle: "tombstone", paused: false, source_root: "/sources/x" }] }),
    }) });
    await screen.findByRole("heading", { name: "Protection", level: 2 });
    await waitFor(() => expect(screen.queryByRole("heading", { name: TITLE, level: 2 })).toBeNull());
  });

  it("names every message in both languages", () => {
    for (const locale of ["en", "it"] as const) {
      const messages = loadCatalog(locale);
      for (const key of ["title", "summary", "warning", "enable", "off_hint", "reauth_hint", "how", "only_copy", "grace_days", "grace_help",
        "cap_files", "cap_gib", "cap_help", "folders", "folders_help", "folders_whole", "folders_some", "save", "saved",
        "never_run", "last_run", "next_run", "outcome.scheduled_one", "outcome.scheduled_other", "outcome.none",
        "outcome.capped_one", "outcome.capped_other", "outcome.skipped", "outcome.none_waiting_one", "outcome.none_waiting_other",
        "outcome.none_unseen_one", "outcome.none_unseen_other", "outcome.none_held_one", "outcome.none_held_other",
        "current_none", "current_one", "current_other", "current_unknown", "waiting_one", "waiting_other", "ready_one", "ready_other",
        "beyond_minimum", "beyond_minimum_help", "held_one", "held_other", "unseen_one", "unseen_other", "totals_one", "totals_other",
        "cloud_life", "cloud_life_beyond", "cloud_life_within", "paused_hint", "pending_one", "pending_other", "cancel", "cancelled"]) {
        expect(messages[`mirror_cleanup.${key}`], `${locale} ${key}`).toBeTruthy();
      }
    }
    expect(loadCatalog("it")["mirror_cleanup.warning"]).toBe(
      "Il cloud non conserverà più i file eliminati dal server: non protegge da cancellazioni accidentali. Adatto a copie che sono già backup, non a dati originali.");
  });

  it("links a scheduled notice to the settings and a capped Problem to the Cloud-only files", () => {
    expect(problemLink(3, { mirror_cleanup: true, group_id: "m-1" })).toBe(vaultSettingsUrl(3, "mirror"));
    expect(problemLink(3, { mirror_cleanup: true, count: 9000 })).toBe(vaultFilesUrl(3, { state: "cloud_only" }));
  });
});
