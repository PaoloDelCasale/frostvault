import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { VaultDeletionDialog } from "@/components/VaultDeletionDialog";
import { problemLink } from "@/lib/problemLink";
import { formatDateTime } from "@/pages/archive/format";

const messages = JSON.parse(readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app/locales/en.json"), "utf8"));
const PREFIX = "vaults/2f1c7e36-0000-4000-8000-000000000001/";
const preview = {
  prefix: PREFIX, object_count: 3, version_count: 5, delete_marker_count: 1, multipart_upload_count: 1,
  byte_count: 3 * 1024 ** 3, early_deletion_cost: 4.5, currency: "EUR", pricing_effective_at: "2026-01-01",
  region: "eu-south-1", executes_at: "2026-11-23T12:00:00+00:00", delay_seconds: 7 * 86400,
  by_storage_class: [
    { storage_class: "STANDARD", version_count: 3, byte_count: 1024 ** 3, early_deletion_cost: 0, early_deletion_version_count: 0, minimum_days: 0 },
    { storage_class: "DEEP_ARCHIVE", version_count: 2, byte_count: 2 * 1024 ** 3, early_deletion_cost: 4.5, early_deletion_version_count: 2, minimum_days: 180 },
  ],
};
const idle = { vault_id: 2, vault_name: "Photos", lifecycle: "tombstone", deletable: true, local_files: 4, request: null };
const pendingUntil = new Date(Date.now() + (6 * 24 + 5) * 3_600_000 - 60_000).toISOString();
const request = { status: "pending", requested_at: "2026-11-16T12:00:00+00:00", pending_until: pendingUntil,
  reason: "No longer needed", attempts: 0, failed_at: null, failure: null, preview, cancellable: true, retryable: false };
const pending = { ...idle, request };
const failed = { ...idle, request: { ...request, status: "failed", attempts: 1, failed_at: "2026-11-23T12:05:00+00:00",
  failure: { reason: "leftovers", leftover_versions: 2, leftover_markers: 1, leftover_uploads: 0 }, retryable: true } };

afterEach(() => { cleanup(); resetApiClientForTests(); });

type Reply = { status?: number; body: unknown };

function setup({ status = idle as unknown, previewReply = { body: preview } as Reply, onCompleted = vi.fn() } = {}) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  configureApiClient({ fetch: vi.fn(async (input, init) => {
    const url = String(input); const method = init?.method ?? "GET";
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });
    const reply: Reply = url.startsWith("/api/i18n/catalog") ? { body: { locale: "en", locales: ["en", "it"], messages } }
      : url.endsWith("/preview") ? previewReply
      : url.endsWith("/cancel") ? { body: idle }
      : url.endsWith("/retry") ? { body: { ...pending, request: { ...request, attempts: 1 } } }
      : method === "POST" ? { status: 202, body: pending }
      : status === 404 ? { status: 404, body: { detail: "Vault not found" } }
      : { body: status };
    return new Response(JSON.stringify(reply.body), { status: reply.status ?? 200, headers: { "Content-Type": "application/json" } });
  }) });
  const onOpenChange = vi.fn();
  render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider>
    <VaultDeletionDialog open onOpenChange={onOpenChange} onCompleted={onCompleted} vaultId={2} vaultName="Photos" />
  </I18nProvider></ApiQueryProvider>);
  return { calls, onOpenChange, onCompleted };
}

describe("Vault Deletion of a Tombstone (#717)", () => {
  // #731: counted from the Class Entry; an older stored preview has no counts and no line.
  it("says how many versions are still within their minimum duration and from when deleting saves", async () => {
    setup({ previewReply: { body: { ...preview, early_deletion_version_count: 1200, early_deletion_byte_count: 2 * 1024 ** 3,
      savings_from: "2027-05-15T10:00:00+00:00", early_deletion_estimated_count: 0,
      by_storage_class: preview.by_storage_class.map((entry) => ({ ...entry, early_deletion_byte_count: entry.early_deletion_version_count ? 2 * 1024 ** 3 : 0 })) } } });
    const line = await screen.findByTestId("vault-deletion-minimum");
    expect(line).toHaveTextContent("1,200 versions (2.0 GiB) have been in Deep Archive for less than 180 days.");
    expect(line).toHaveTextContent(/Deleting them now saves nothing: AWS still bills the remaining days \(~€4\.50\)\. Savings start on May 15, 2027\./);
    expect(line).toHaveTextContent("Estimate from AWS list prices, not from your bill.");
  });

  it("names estimated entries as versions, not files", async () => {
    setup({ previewReply: { body: { ...preview, early_deletion_version_count: 1200, early_deletion_byte_count: 2 * 1024 ** 3,
      savings_from: "2027-05-15T10:00:00+00:00", early_deletion_estimated_count: 1200,
      by_storage_class: preview.by_storage_class.map((entry) => ({ ...entry, early_deletion_byte_count: entry.early_deletion_version_count ? 2 * 1024 ** 3 : 0 })) } } });
    const line = await screen.findByTestId("vault-deletion-minimum");
    expect(line).toHaveTextContent("Class entry estimated from the storage plan for 1,200 versions.");
    expect(line).not.toHaveTextContent(/1,200 files/);
  });

  // #736: a Retention Lock defers locked versions, never fails the deletion.
  it("says how many versions a Retention Lock still holds and from when they can be deleted", async () => {
    setup({ previewReply: { body: { ...preview, retention_lock: { days: 180, mode: "GOVERNANCE", locked_count: 3,
      locked_bytes: 1024 ** 3, locked_from: "2027-02-01T10:00:00+00:00", locked_until: "2027-04-01T10:00:00+00:00" } } } });
    const line = await screen.findByTestId("vault-deletion-retention-lock");
    expect(line).toHaveTextContent("Retention Lock: 3 versions (1.0 GiB) will still be locked.");
    expect(line).toHaveTextContent("Deletable from Feb 1, 2027 (the last from Apr 1, 2027)");
  });

  it("shows a deferred deletion as waiting for its locked versions, not as failed", async () => {
    setup({ status: { ...pending, request: { ...request, attempts: 1, retained_count: 2,
      retained_until: "2027-04-01T10:00:00+00:00", next_attempt_at: "2027-02-01T10:00:00+00:00" } } });
    const waiting = await screen.findByTestId("vault-deletion-retained");
    expect(waiting).toHaveTextContent("Only 2 versions under Retention Lock are left: the last is deletable from Apr 1, 2027.");
    expect(waiting).toHaveTextContent("this is not an error");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByTestId("vault-deletion-countdown")).toBeNull();
  });

  it("shows no minimum duration line for a preview stored before it", async () => {
    setup();
    await screen.findByTestId("vault-deletion-preview");
    expect(screen.queryByTestId("vault-deletion-minimum")).toBeNull();
  });

  it("shows the live preview and requires the exact name and a reason before scheduling", async () => {
    const user = userEvent.setup(); const { calls } = setup();
    const summary = await screen.findByTestId("vault-deletion-preview");
    expect(screen.getByTestId("vault-deletion-local-note")).toHaveTextContent("Local files stay where they are, but they will no longer have a copy in the cloud.");
    expect(screen.getByText(new RegExp(`Every version and delete marker under ${PREFIX}`))).toBeInTheDocument();
    expect(summary).toHaveTextContent("In the cloud now: 3 objects, 5 versions, 1 delete marker");
    expect(summary).toHaveTextContent("DEEP_ARCHIVE");
    expect(screen.getByText("Also 1 unfinished upload, aborted with the rest.")).toBeInTheDocument();
    expect(screen.getByTestId("vault-deletion-early-cost")).toHaveTextContent(/Early deletion charge \(estimate\): .*4[.,]50/);
    expect(screen.getByText("4 files of this Vault are still on the server: they stay there.")).toBeInTheDocument();
    expect(screen.getByText("A 7-day cancellable delay applies before any deletion call.")).toBeInTheDocument();
    expect(screen.getByText("A recent reauthentication is required.")).toBeInTheDocument();

    const submit = screen.getByRole("button", { name: "Delete the Vault in seven days" });
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText("Reason"), "No longer needed");
    await user.type(screen.getByLabelText("Type the exact Vault name (Photos)"), "photos");
    expect(submit).toBeDisabled();
    await user.clear(screen.getByLabelText("Type the exact Vault name (Photos)"));
    await user.type(screen.getByLabelText("Type the exact Vault name (Photos)"), "Photos");
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    const status = await screen.findByTestId("vault-deletion-status");
    expect(status).toHaveAttribute("data-status", "pending");
    expect(calls.find((c) => c.method === "POST")).toEqual({ url: "/api/vaults/2/vault-deletion", method: "POST",
      body: { confirmation: "Photos", reason: "No longer needed", delete_bucket: false } });
    expect(status).toHaveTextContent(`Vault Deletion scheduled: it cannot start before ${formatDateTime(pendingUntil)}.`);
    expect(screen.getByTestId("vault-deletion-countdown")).toHaveTextContent("Time left to cancel: 6 days 5 hours.");
  });

  it("cancels a pending deletion and reports the change only when closed", async () => {
    const user = userEvent.setup(); const { calls, onCompleted } = setup({ status: pending });
    await user.click(await screen.findByRole("button", { name: "Cancel Vault Deletion" }));
    await screen.findByRole("button", { name: "Delete the Vault in seven days" });
    expect(calls.some((c) => c.url === "/api/vaults/2/vault-deletion/cancel" && c.method === "POST")).toBe(true);
    expect(onCompleted).not.toHaveBeenCalled();
    await user.click(screen.getAllByRole("button", { name: "Close" }).at(-1)!);
    expect(onCompleted).toHaveBeenCalledOnce();
  });

  it("refuses confirmation when the listing fails and offers to list again", async () => {
    const user = userEvent.setup();
    const { calls } = setup({ previewReply: { status: 502, body: { detail: "listing failed", message_key: "api.vault_deletion.listing_failed" } } });
    await screen.findByTestId("vault-deletion-preview-failed");
    expect(screen.getByText("The Vault's cloud content could not be listed, so the deletion cannot be confirmed.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Reason"), "No longer needed");
    await user.type(screen.getByLabelText("Type the exact Vault name (Photos)"), "Photos");
    expect(screen.getByRole("button", { name: "Delete the Vault in seven days" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "List again" }));
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith("/preview"))).toHaveLength(2));
  });

  it("keeps a failed deletion visible with what is left, and retries at once", async () => {
    const user = userEvent.setup(); const { calls } = setup({ status: failed });
    const status = await screen.findByTestId("vault-deletion-status");
    expect(status).toHaveAttribute("data-status", "failed");
    expect(status).toHaveTextContent("The Vault Deletion did not complete: the Vault stays archived with its catalog.");
    expect(status).toHaveTextContent("3 items still remain under the Vault prefix.");
    expect(screen.getByRole("button", { name: "Cancel Vault Deletion" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Retry now" }));
    await waitFor(() => expect(screen.getByTestId("vault-deletion-status")).toHaveAttribute("data-status", "pending"));
    expect(calls.some((c) => c.url === "/api/vaults/2/vault-deletion/retry" && c.method === "POST")).toBe(true);
  });

  it("says the Vault is gone once it has left the catalog", async () => {
    setup({ status: 404 });
    expect(await screen.findByTestId("vault-deletion-deleted")).toHaveTextContent("The Vault was deleted. Only its audit record remains.");
  });

  // #739: only a dedicated Vault Bucket (empty prefix) offers to delete the bucket, unticked.
  it("does not offer to delete the shared bucket of an older Vault", async () => {
    setup();
    await screen.findByTestId("vault-deletion-preview");
    expect(screen.queryByLabelText("Also delete the bucket")).toBeNull();
  });

  it("offers to delete a dedicated Vault Bucket, unticked by default, and sends the choice", async () => {
    const user = userEvent.setup(); const { calls } = setup({ previewReply: { body: { ...preview, prefix: "" } } });
    const box = await screen.findByLabelText("Also delete the bucket");
    expect(box).not.toBeChecked();
    await user.click(box);
    await user.type(screen.getByLabelText("Reason"), "No longer needed");
    await user.type(screen.getByLabelText("Type the exact Vault name (Photos)"), "Photos");
    await user.click(screen.getByRole("button", { name: "Delete the Vault in seven days" }));
    await screen.findByTestId("vault-deletion-status");
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ confirmation: "Photos", reason: "No longer needed", delete_bucket: true });
  });

  it("says the dedicated bucket stays when the Owner did not tick it", async () => {
    setup({ status: { ...pending, request: { ...request, preview: { ...preview, prefix: "" }, delete_bucket: false } } });
    expect(await screen.findByTestId("vault-deletion-bucket")).toHaveTextContent("The Vault Bucket stays, empty.");
  });

  it("links a Vault Deletion Problem to the archived Vaults", () => {
    expect(problemLink(2, { vault: "Photos", vault_deletion: true })).toBe("/#archived");
    expect(problemLink(2, { path: "a/b.txt" })).not.toBe("/#archived");
  });
});
