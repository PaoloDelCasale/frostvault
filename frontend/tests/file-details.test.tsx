import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { configureApiClient, resetApiClientForTests } from "@/api";
import { FileDetails } from "@/pages/archive/FileDetails";
import { jsonResponse } from "./support/http";
import { renderWithQueryClient } from "./support/queryClient";

const t = (key: string) => key;
const stale = { type: "file" as const, name: "same.jpg", path: "same.jpg", state: "cloud_only" };
let purgeFailure: Record<string, unknown> | null = null;
let versionExtra: Record<string, unknown> = {};
function setup(state: string, local: boolean, conflict = false, reconciliation = false, remediation = false, extra: Record<string, unknown> = {}, pending = false) {
  const calls: string[] = [];
  configureApiClient({ fetch: vi.fn(async (input) => {
    const url = String(input); calls.push(url);
    if (/^\/api\/vaults\/\d+\/files\/detail\?/.test(url)) return jsonResponse({ ...stale, state, local_exists: local, cloud_exists: true, cloud_only_conflict: conflict, ...extra });
    if (/^\/api\/vaults\/\d+\/files\/versions/.test(url)) return jsonResponse({ items: [{ id: "v", recoverable: true, storage_class: "STANDARD", provider_version_id: "exact", verified_at: "2026-11-05", integrity: "verified", ...versionExtra }], ...(reconciliation ? { reconciliation: { pending: true, can_resume: true, reason: "orphan_different", remedy: "keep copies" } } : {}), ...(remediation ? { remediations: [{ id: "r", state: "blocked", reason: "cleanup_unavailable", can_resume: true, archive_version_id: "snapshot-A", current_job_id: 8, resolved_by_job_id: null, original_job_ids: [1] }] } : {}) });
    if (/^\/api\/vaults\/\d+\/files\/jobs/.test(url)) return jsonResponse({ items: [{ id: url.includes("before_id") ? 1 : 100, path: "same.jpg", action: "upload", status: "failed", message: "Historical failure", requested_at: "2026-11-01" }], next_before_id: url.includes("before_id") ? null : 100 });
    if (/^\/api\/vaults\/\d+\/jobs\//.test(url)) return jsonResponse({ id: 1, path: "same.jpg", action: "upload", status: "failed", message: "Historical failure", remediation: { state: "resolved", current_job_id: 8, resolved_by_job_id: 8, original_job_ids: [1] } });
    if (/^\/api\/vaults\/\d+\/jobs/.test(url)) return jsonResponse({ groups: pending ? [{ id: "old-folder-delete", path: "parent-folder", action: "cloud-purge", status: "pending_delay" }] : [] });
    if (/^\/api\/vaults\/\d+\/cloud-purge/.test(url) && purgeFailure) return jsonResponse(purgeFailure, 409);
    if (/^\/api\/vaults\/\d+\/cloud-deletion\/preview/.test(url)) return jsonResponse({ object_count: 1, version_count: 1, delete_marker_count: 0, byte_count: 12 });
    if (url.startsWith("/api/cloud-deletion")) return jsonResponse({ purge_delay_seconds: 604800 });
    return jsonResponse({});
  }) });
  renderWithQueryClient(<FileDetails target={{ item: stale, vaultId: 2, vaultName: "Second" }} t={t} onClose={vi.fn()} onRecover={vi.fn()} onRefresh={vi.fn()} />);
  return calls;
}
afterEach(() => { resetApiClientForTests(); purgeFailure = null; versionExtra = {}; });
describe("deliberate file Details", () => {
  it("separates verified cloud cleanup from changed Local Copy protection and offers exact resume", async () => {
    const calls = setup("pending", true, false, false, true);
    await screen.findByText("remediation.target: snapshot-A");
    expect(screen.queryByText(/remediation\.phase/)).not.toBeInTheDocument();
    expect(screen.getByText("remediation.reason: reconciliation.reason.cleanup_unavailable")).toBeInTheDocument();
    expect(screen.queryByText("remediation.reason: cleanup_unavailable")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("details-summary")).getByText("state.pending")).toHaveAttribute("data-state", "pending");
    await userEvent.setup().click(screen.getByRole("button", { name: "reconciliation.resume" }));
    await waitFor(() => expect(calls).toContain("/api/vaults/2/upload/reconcile"));
    expect(calls.some((url) => /^\/api\/vaults\/\d+\/upload$/.test(url))).toBe(false);
  });
  it("shows the class the storage plan expects when the transition is not observed yet (#731)", async () => {
    versionExtra = { expected_storage_class: "DEEP_ARCHIVE", storage_class_since: "2026-11-02T00:00:00+00:00",
      storage_class_since_estimated: true, minimum_storage_until: "2027-05-01T23:30:00+00:00" };
    setup("cloud_only", false);
    const summary = await screen.findByTestId("details-summary");
    expect(within(summary).getByTestId("expected-storage-class")).toHaveTextContent("storage.DEEP_ARCHIVE");
    expect(within(summary).getByText("details.expected_class_help")).toBeInTheDocument();
    expect(within(summary).getByTestId("minimum-storage-until")).toHaveTextContent("(details.estimated)");
  });
  it("shows no expected class when the recorded class is the projection", async () => {
    versionExtra = { expected_storage_class: null, minimum_storage_until: null };
    setup("cloud_only", false);
    const summary = await screen.findByTestId("details-summary");
    expect(within(summary).queryByTestId("expected-storage-class")).toBeNull();
    expect(within(summary).queryByTestId("minimum-storage-until")).toBeNull();
  });
  it("explains an excluded file as neither pending nor a problem", async () => {
    setup("excluded", true);
    expect(within(await screen.findByTestId("details-summary")).getByText("state.excluded")).toHaveAttribute("data-state", "excluded");
    expect(screen.getByText("essential.excluded_help")).toBeInTheDocument();
    expect(screen.queryByText("essential.open_problem")).not.toBeInTheDocument();
    expect(screen.queryByTestId("excluded-folder-help")).not.toBeInTheDocument();
  });
  it("says a file in an Excluded Folder is not observed (#688)", async () => {
    setup("excluded", true, false, false, false, { in_excluded_folder: true });
    expect(within(await screen.findByTestId("details-summary")).getByText("state.excluded · state.not_observed_excluded_folder")).toHaveAttribute("data-state", "excluded");
    expect(screen.getByTestId("excluded-folder-help")).toHaveTextContent("essential.excluded_folder_help");
  });
  it("explains a Stopped Upload and uploads it now on request (#630)", async () => {
    const calls = setup("stopped", true, false, false, false, { upload_eligible: true });
    expect(within(await screen.findByTestId("details-summary")).getByText("state.stopped")).toHaveAttribute("data-state", "stopped");
    const help = screen.getByTestId("stopped-help");
    expect(help).toHaveTextContent("essential.stopped_help");
    expect(screen.queryByText("essential.open_problem")).not.toBeInTheDocument();
    await userEvent.setup().click(within(help).getByRole("button", { name: "essential.upload_now" }));
    await waitFor(() => expect(calls).toContain("/api/vaults/2/upload"));
  });
  it.each([["symlink", "essential.unsupported_symlink_help"], ["other", "essential.unsupported_other_help"]])("explains an unsupported %s entry by its cause, not as a problem (#524)", async (type, help) => {
    setup("unsupported", true, false, false, false, { local_file_type: type, cloud_exists: 0 });
    expect(within(await screen.findByTestId("details-summary")).getByText("state.unsupported")).toHaveAttribute("data-state", "unsupported");
    expect(screen.getByTestId("unsupported-help")).toHaveTextContent(help);
    expect(screen.queryByText("essential.open_problem")).not.toBeInTheDocument();
    // A falsy numeric cloud_exists must not render a stray "0".
    expect(within(screen.getByTestId("details-summary")).queryByText("0")).not.toBeInTheDocument();
  });
  it("summarises copies and recovery first, with technical details collapsed and translated", async () => {
    setup("cloud_only", false);
    const summary = await screen.findByTestId("details-summary");
    expect(within(summary).getByText("details.on_server").nextSibling).toHaveTextContent("details.absent");
    expect(within(summary).getByText("details.in_cloud").nextSibling).toHaveTextContent("details.present");
    expect(within(summary).getByText("details.recovery_time").nextSibling).toHaveTextContent("essential.ready_now");
    const technical = screen.getByText("details.technical").closest("details")!;
    expect(technical).not.toHaveAttribute("open");
    expect(within(technical).getByText("integrity.verified")).toBeInTheDocument();
    expect(within(technical).getByText("exact")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveAccessibleName("same.jpg");
  });
  it("shows a Recovery in progress with its tier and expected time instead of offering a new one (#646)", async () => {
    setup("cloud_only", false, false, false, false, { recover_eligible: false, recovery: { state: "waiting_for_cloud", active: true, tier: "Standard", days: 1, requested_at: "2026-11-14T09:57:00Z", ready_by: "2026-11-14T21:57:00Z", expires_at: null } });
    const progress = await screen.findByTestId("recovery-progress");
    // Tier and expected time show once, in «Recovery time» (#677).
    expect(progress).toHaveTextContent(/^recovery\.in_progress · recovery\.state\.waiting_for_cloud · recovery\.requested_at$/);
    const summary = screen.getByTestId("details-summary");
    expect(within(summary).getByText("state.recovering")).toHaveAttribute("data-state", "restoring");
    expect(within(summary).getByText("details.recovery_time").nextSibling).toHaveTextContent("recovery.tier.Standard · recovery.ready_by");
    expect(screen.queryByRole("button", { name: "ui.row_action_recover" })).not.toBeInTheDocument();
  });
  it("keeps the stopped-wait sentence apart from the Recovery details (#677)", async () => {
    setup("cloud_only", false, false, false, false, { recover_eligible: true, recovery: { state: "waiting_for_cloud", active: false, tier: "Standard", days: 1, requested_at: "2026-11-14T09:57:00Z", ready_by: "2026-11-14T21:57:00Z", expires_at: null } });
    const note = await screen.findByTestId("recovery-progress");
    const [sentence, details] = within(note).getAllByText(/./);
    expect(sentence).toHaveTextContent(/^recovery\.cloud_continues$/);
    expect(details).toHaveTextContent(/^recovery\.requested_at$/);
    expect(within(screen.getByTestId("details-summary")).getByText("details.recovery_time").nextSibling).toHaveTextContent("recovery.tier.Standard · recovery.ready_by");
  });
  it("says until when a restored copy stays available and keeps Recover", async () => {
    setup("cloud_only", false, false, false, false, { recover_eligible: true, recovery: { state: "ready", active: false, tier: "Bulk", days: 3, requested_at: null, ready_by: null, expires_at: "2026-11-17T00:00:00Z" } });
    expect(await screen.findByTestId("recovery-progress")).toHaveTextContent("recovery.copy_until");
    expect(screen.getByRole("button", { name: "ui.row_action_recover" })).toBeInTheDocument();
    expect(screen.queryByText("state.recovering")).not.toBeInTheDocument();
  });
  it("loads paginated targeted history and bidirectional links outside recent50", async () => {
    const calls = setup("protected",true);
    const user = userEvent.setup();
    await screen.findByText("exact");
    await user.click(screen.getByText("remediation.history"));
    await screen.findByText("Nov 1, 2026 · Historical failure");
    await user.click(screen.getByRole("button", { name: "remediation.older" }));
    await waitFor(() => expect(calls).toContain("/api/vaults/2/files/jobs?path=same.jpg&before_id=100"));
    await user.click(screen.getByRole("button", { name: "remediation.job · operation.failed" }));
    await waitFor(() => expect(calls).toContain("/api/vaults/2/jobs/1"));
    expect(screen.getByRole("button", { name: "remediation.original" })).toBeInTheDocument();
  });
  it("offers scope-pinned exact reconciliation even when protected and normal upload is ineligible", async () => {
    const calls = setup("protected", true, false, true);
    await screen.findByText("reconciliation.reason.orphan_different");
    expect(screen.queryByRole("button", { name: "essential.retry" })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "reconciliation.resume" }));
    await waitFor(() => expect(calls).toContain("/api/vaults/2/upload/reconcile"));
    expect(calls.some((url) => /^\/api\/vaults\/\d+\/upload$/.test(url))).toBe(false);
    expect(screen.queryByText("essential.danger_zone")).not.toBeInTheDocument();
  });
  it("revalidates stale Cloud-only selection and never offers cloud deletion for a present local copy", async () => {
    const calls = setup("protected", true);
    await screen.findByText("exact");
    expect(screen.queryByText("essential.danger_zone")).not.toBeInTheDocument();
    expect(calls.every((url) => url.startsWith("/api/vaults/2/"))).toBe(true);
    expect(calls.some((url) => /^\/api\/vaults\/\d+\/cloud-purge/.test(url))).toBe(false);
  });
  it("finds an old pending deletion by exact file membership, even in a folder group outside recent50", async () => {
    const calls = setup("cloud_only", false, false, false, false, {}, true);
    await screen.findByText("essential.deletion_pending");
    expect(calls).toContain("/api/vaults/2/jobs?action=cloud-purge&active_only=true&path=same.jpg");
    expect(screen.queryByText("essential.danger_zone")).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "essential.cancel_deletion" }));
    await waitFor(() => expect(calls).toContain("/api/vaults/2/jobs/cancel"));
  });
  it("holds a conflict and explains external rename or explicit replacement without list deletion", async () => {
    setup("problem", true, true);
    await screen.findByText("essential.conflict_help");
    expect(screen.getByRole("button", { name: "essential.replace_cloud" })).toBeInTheDocument();
    expect(screen.queryByText("essential.danger_zone")).not.toBeInTheDocument();
  });
  it("cloud deletion starts only inside deliberate Details, with a scope-pinned preview and typed confirmation", async () => {
    const calls = setup("cloud_only", false);
    await screen.findByText("exact");
    const user = userEvent.setup(); await user.click(screen.getByText("essential.danger_zone"));
    await user.click(screen.getByRole("button", { name: "ui.cloud_purge_title" }));
    await screen.findByTestId("cloud-purge-form");
    expect(screen.getByTestId("cloud-purge-submit")).toBeDisabled();
    expect(calls).toContain("/api/vaults/2/cloud-deletion/preview");
    await user.type(screen.getByTestId("cloud-purge-reason"), "Deliberate removal");
    await user.type(screen.getByTestId("cloud-purge-confirmation"), "Second");
    await user.click(screen.getByTestId("cloud-purge-submit"));
    await waitFor(() => expect(calls).toContain("/api/vaults/2/cloud-purge"));
  });
  it("shows a cloud deletion failure inside the open dialog, not in the hidden Details sheet (#651)", async () => {
    purgeFailure = { detail: "Bucket refused the deletion", code: "conflict", message_key: "api.cloud_purge_refused", message_params: {} };
    const calls = setup("cloud_only", false);
    await screen.findByText("exact");
    const user = userEvent.setup(); await user.click(screen.getByText("essential.danger_zone"));
    await user.click(screen.getByRole("button", { name: "ui.cloud_purge_title" }));
    const dialog = await screen.findByRole("dialog", { name: "ui.cloud_purge_title" });
    await user.type(within(dialog).getByTestId("cloud-purge-reason"), "Deliberate removal");
    await user.type(within(dialog).getByTestId("cloud-purge-confirmation"), "Second");
    await user.click(within(dialog).getByTestId("cloud-purge-submit"));
    await waitFor(() => expect(calls).toContain("/api/vaults/2/cloud-purge"));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Bucket refused the deletion");
    expect(alert).not.toHaveTextContent("ApiError");
    expect(screen.getByRole("dialog", { name: "ui.cloud_purge_title" })).toBeInTheDocument();
    expect(within(dialog).getByTestId("cloud-purge-submit")).toHaveTextContent("ui.cloud_purge_submit");
    expect(within(dialog).getByTestId("cloud-purge-submit")).toBeEnabled();
  });
});
