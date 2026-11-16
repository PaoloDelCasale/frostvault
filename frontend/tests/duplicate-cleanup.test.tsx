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
    expect(within(section).getByLabelText("File cap per run")).toHaveValue("5000");
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
      for (const key of ["title", "summary", "enable", "off_hint", "how", "never_only_cloud", "save", "pending_one", "pending_other"]) {
        expect(messages[`duplicate_cleanup.${key}`], `${locale} ${key}`).toBeTruthy();
      }
    }
  });

  it("links a scheduled notice to the settings and a capped Problem to the Cloud-only files", () => {
    expect(problemLink(3, { duplicate_cleanup: true, group_id: "g-1" })).toBe(vaultSettingsUrl(3, "duplicates"));
    expect(problemLink(3, { duplicate_cleanup: true, count: 9000 })).toBe(vaultFilesUrl(3, { state: "cloud_only" }));
  });
});
