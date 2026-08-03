import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { useState } from "react";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
  type VaultDecommissionPreview,
  type VaultDecommissionStatus,
} from "@/api";
import { DecommissionVaultDialog } from "@/components/DecommissionVaultDialog";
import { I18nProvider } from "@/i18n/I18nProvider";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const en = JSON.parse(
  readFileSync(path.join(root, "app/locales/en.json"), "utf8"),
) as Record<string, string>;
const itMessages = JSON.parse(
  readFileSync(path.join(root, "app/locales/it.json"), "utf8"),
) as Record<string, string>;

function response(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function preview(overrides: Partial<VaultDecommissionPreview> = {}): VaultDecommissionPreview {
  return {
    vault_id: 7,
    vault_name: "Exact Archive",
    enabled: true,
    decommission_state: "active",
    local_disposition: "retain",
    cloud_disposition: "retain",
    counts: {
      vault_files: 3,
      local_files: 2,
      local_bytes: 1024,
      archive_versions: 4,
      cloud_bytes: 2048,
      delete_markers: 1,
      jobs: 8,
      memberships: 2,
    },
    blockers: [],
    can_start: true,
    fingerprint: "a".repeat(64),
    ...overrides,
  };
}

function status(): VaultDecommissionStatus {
  return {
    id: 1,
    vault_id: 7,
    vault_name: "Exact Archive",
    state: "cloud_purge",
    decommission_state: "decommissioning",
    enabled: true,
    local_disposition: "retain",
    cloud_disposition: "purge",
    local_status: "retained",
    cloud_status: "purging",
    requested_at: "2026-08-01T10:00:00Z",
    updated_at: "2026-08-01T10:01:00Z",
    root_released: false,
    preview: preview({ cloud_disposition: "purge" }),
    jobs: {
      local: { total: 0, completed: 0, failed: 0, cancelled: 0, active: 0 },
      cloud: { total: 4, completed: 1, failed: 0, cancelled: 0, active: 3 },
    },
    progress_percent: 40,
  };
}

async function renderDialog(options?: {
  previewValue?: VaultDecommissionPreview;
  startValue?: VaultDecommissionStatus;
}) {
  resetApiClientForTests();
  configureApiClient({
    fetch: vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).startsWith("/api/i18n/catalog")) {
        return response({ locale: "en", locales: ["en", "it"], messages: en });
      }
      throw new Error(`Unexpected request ${String(input)}`);
    }),
  });
  const requestPreview = vi.fn(async () => options?.previewValue ?? preview());
  const requestStart = vi.fn(async () => options?.startValue ?? status());
  const requestStatus = vi.fn(async () => options?.startValue ?? status());
  function ReopenableDialog() {
    const [open, setOpen] = useState(true);
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>Reopen decommission</button>
        <DecommissionVaultDialog
          open={open}
          vaultName={open ? "Exact Archive" : ""}
          existingState="active"
          onOpenChange={setOpen}
          preview={requestPreview}
          start={requestStart}
          status={requestStatus}
        />
      </>
    );
  }
  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <ReopenableDialog />
      </I18nProvider>
    </ApiQueryProvider>,
  );
  await screen.findByRole("dialog", { name: /decommission vault/i });
  return { requestPreview, requestStart, requestStatus };
}

afterEach(() => {
  cleanup();
  resetApiClientForTests();
});

describe("Vault decommission confirmation and progress", () => {
  it("shows authoritative counts/fingerprint and sends four explicit gates", async () => {
    const user = userEvent.setup();
    const harness = await renderDialog();
    const dialog = screen.getByRole("dialog");

    await within(dialog).findByText(/3 Vault Files/i);
    expect(within(dialog).getByText(/1 Delete Markers/i)).toBeInTheDocument();
    expect(within(dialog).getByTestId("decommission-fingerprint")).toHaveTextContent(
      "a".repeat(64),
    );

    await user.click(within(dialog).getByLabelText(/Permanently purge cloud history/i));
    await waitFor(() => expect(harness.requestPreview).toHaveBeenLastCalledWith({
      local_disposition: "retain",
      cloud_disposition: "purge",
    }));

    await user.type(within(dialog).getByLabelText(/Mandatory reason/i), "retire archive");
    await user.type(
      within(dialog).getByLabelText(/Type the exact Vault name/i),
      "exact archive",
    );
    expect(within(dialog).getByRole("button", { name: /Start irreversible/i })).toBeDisabled();
    await user.clear(within(dialog).getByLabelText(/Type the exact Vault name/i));
    await user.type(
      within(dialog).getByLabelText(/Type the exact Vault name/i),
      "Exact Archive",
    );
    await user.click(within(dialog).getByRole("button", { name: /Start irreversible/i }));

    await waitFor(() => expect(harness.requestStart).toHaveBeenCalledWith({
      local_disposition: "retain",
      cloud_disposition: "purge",
      confirmation: "Exact Archive",
      reason: "retire archive",
      preview_fingerprint: "a".repeat(64),
    }));
    expect(await screen.findByTestId("decommission-progress")).toHaveTextContent(
      /Permanent cloud purge in progress/i,
    );
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "40");
    expect(screen.getByText(/root remains reserved/i)).toBeInTheDocument();
  });

  it("reloads in-progress status instead of stale preview when reopened", async () => {
    const user = userEvent.setup();
    const harness = await renderDialog();
    await user.type(screen.getByLabelText(/Mandatory reason/i), "retire archive");
    await user.type(screen.getByLabelText(/Type the exact Vault name/i), "Exact Archive");
    await user.click(screen.getByRole("button", { name: /Start irreversible/i }));
    await screen.findByTestId("decommission-progress");

    await user.click(screen.getByText("Close", { selector: "button" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const previewsBeforeReopen = harness.requestPreview.mock.calls.length;
    const statusesBeforeReopen = harness.requestStatus.mock.calls.length;
    await user.click(screen.getByRole("button", { name: /Reopen decommission/i }));

    expect(await screen.findByTestId("decommission-progress")).toHaveTextContent(
      /Permanent cloud purge in progress/i,
    );
    expect(harness.requestStatus.mock.calls.length).toBeGreaterThan(statusesBeforeReopen);
    expect(harness.requestPreview).toHaveBeenCalledTimes(previewsBeforeReopen);
  });

  it("renders blockers and cannot submit a destructive choice", async () => {
    const blocked = preview({
      can_start: false,
      blockers: [
        {
          code: "active_jobs",
          message: "active",
          message_key: "decommission.blocker.active_jobs",
          count: 2,
        },
      ],
    });
    const user = userEvent.setup();
    const harness = await renderDialog({ previewValue: blocked });
    expect(await screen.findByRole("alert")).toHaveTextContent(/Active or pending Jobs/i);
    await user.type(screen.getByLabelText(/Mandatory reason/i), "retire archive");
    await user.type(screen.getByLabelText(/Type the exact Vault name/i), "Exact Archive");
    expect(screen.getByRole("button", { name: /Start irreversible/i })).toBeDisabled();
    expect(harness.requestStart).not.toHaveBeenCalled();
  });

  it("has distinct English and Italian copy for every decommission key", () => {
    const keys = Object.keys(en).filter((key) => key.startsWith("decommission."));
    expect(keys.length).toBeGreaterThan(40);
    for (const key of keys) {
      expect(itMessages[key], `missing Italian ${key}`).toBeTruthy();
      expect(itMessages[key]).not.toBe(en[key]);
    }
  });
});
