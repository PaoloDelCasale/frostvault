import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import { FileDetails } from "@/pages/archive/FileDetails";
import { jsonResponse } from "./vault-settings-harness";

const t = (key: string) => key;
const stale = { type: "file" as const, name: "same.jpg", path: "same.jpg", state: "cloud_only" };
let purgeFailure: Record<string, unknown> | null = null;
function setup(state: string, local: boolean, conflict = false, reconciliation = false, remediation = false, extra: Record<string, unknown> = {}, pending = false) {
  const calls: string[] = [];
  configureApiClient({ fetch: vi.fn(async (input) => {
    const url = String(input); calls.push(url);
    if (url.startsWith("/api/files/detail")) return jsonResponse({ ...stale, state, local_exists: local, cloud_exists: true, cloud_only_conflict: conflict, ...extra });
    if (url.startsWith("/api/files/versions")) return jsonResponse({ items: [{ id: "v", recoverable: true, storage_class: "STANDARD", provider_version_id: "exact", verified_at: "2026-11-05", integrity: "verified" }], ...(reconciliation ? { reconciliation: { pending: true, can_resume: true, reason: "orphan_different", remedy: "keep copies" } } : {}), ...(remediation ? { remediations: [{ id: "r", state: "blocked", phase: "cleanup", reason: "cleanup_unavailable", can_resume: true, archive_version_id: "snapshot-A", current_job_id: 8, resolved_by_job_id: null, original_job_ids: [1] }] } : {}) });
    if (url.startsWith("/api/files/jobs")) return jsonResponse({ items: [{ id: url.includes("before_id") ? 1 : 100, path: "same.jpg", action: "upload", status: "failed", message: "Historical failure", requested_at: "2026-11-01" }], next_before_id: url.includes("before_id") ? null : 100 });
    if (url.startsWith("/api/jobs/")) return jsonResponse({ id: 1, path: "same.jpg", action: "upload", status: "failed", message: "Historical failure", remediation: { state: "resolved", current_job_id: 8, resolved_by_job_id: 8, original_job_ids: [1] } });
    if (url.startsWith("/api/jobs")) return jsonResponse({ groups: pending ? [{ id: "old-folder-delete", path: "parent-folder", action: "cloud-purge", status: "pending_delay" }] : [] });
    if (url.startsWith("/api/cloud-purge") && purgeFailure) return jsonResponse(purgeFailure, 409);
    if (url.startsWith("/api/cloud-deletion/preview")) return jsonResponse({ object_count: 1, version_count: 1, delete_marker_count: 0, byte_count: 12 });
    if (url.startsWith("/api/cloud-deletion")) return jsonResponse({ purge_delay_seconds: 604800 });
    return jsonResponse({});
  }) });
  render(<ApiQueryProvider client={createAppQueryClient()}><FileDetails target={{ item: stale, vaultId: 2, vaultName: "Second" }} t={t} onClose={vi.fn()} onRecover={vi.fn()} onRefresh={vi.fn()} /></ApiQueryProvider>);
  return calls;
}
afterEach(() => { resetApiClientForTests(); purgeFailure = null; });
describe("deliberate file Details", () => {
  it("separates verified cloud cleanup from changed Local Copy protection and offers exact resume", async () => {
    const calls = setup("pending", true, false, false, true);
    await screen.findByText("remediation.target: snapshot-A");
    expect(screen.getByText("remediation.phase: remediation.phase.cleanup")).toBeInTheDocument();
    expect(screen.getByText("remediation.reason: reconciliation.reason.cleanup_unavailable")).toBeInTheDocument();
    expect(screen.queryByText("remediation.reason: cleanup_unavailable")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("details-summary")).getByText("state.pending")).toHaveAttribute("data-state", "pending");
    await userEvent.setup().click(screen.getByRole("button", { name: "reconciliation.resume" }));
    await waitFor(() => expect(calls).toContain("/api/upload/reconcile?vault_id=2"));
    expect(calls.some((url) => url.startsWith("/api/upload?"))).toBe(false);
  });
  it("explains an excluded file as neither pending nor a problem", async () => {
    setup("excluded", true);
    expect(within(await screen.findByTestId("details-summary")).getByText("state.excluded")).toHaveAttribute("data-state", "excluded");
    expect(screen.getByText("essential.excluded_help")).toBeInTheDocument();
    expect(screen.queryByText("essential.open_problem")).not.toBeInTheDocument();
  });
  it("explains a Stopped Upload and uploads it now on request (#630)", async () => {
    const calls = setup("stopped", true, false, false, false, { upload_eligible: true });
    expect(within(await screen.findByTestId("details-summary")).getByText("state.stopped")).toHaveAttribute("data-state", "stopped");
    const help = screen.getByTestId("stopped-help");
    expect(help).toHaveTextContent("essential.stopped_help");
    expect(screen.queryByText("essential.open_problem")).not.toBeInTheDocument();
    await userEvent.setup().click(within(help).getByRole("button", { name: "essential.upload_now" }));
    await waitFor(() => expect(calls).toContain("/api/upload?vault_id=2"));
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
    expect(progress).toHaveTextContent(/^recovery\.in_progress · recovery\.state\.waiting_for_cloud · recovery\.requested_at · recovery\.tier\.Standard · recovery\.ready_by$/);
    const summary = screen.getByTestId("details-summary");
    expect(within(summary).getByText("state.recovering")).toHaveAttribute("data-state", "restoring");
    expect(within(summary).getByText("details.recovery_time").nextSibling).toHaveTextContent("recovery.tier.Standard · recovery.ready_by");
    expect(screen.queryByRole("button", { name: "ui.row_action_recover" })).not.toBeInTheDocument();
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
    await waitFor(() => expect(calls).toContain("/api/files/jobs?path=same.jpg&before_id=100&vault_id=2"));
    await user.click(screen.getByRole("button", { name: "remediation.job · operation.failed" }));
    await waitFor(() => expect(calls).toContain("/api/jobs/1?vault_id=2"));
    expect(screen.getByRole("button", { name: "remediation.original" })).toBeInTheDocument();
  });
  it("offers scope-pinned exact reconciliation even when protected and normal upload is ineligible", async () => {
    const calls = setup("protected", true, false, true);
    await screen.findByText("reconciliation.reason.orphan_different");
    expect(screen.queryByRole("button", { name: "essential.retry" })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "reconciliation.resume" }));
    await waitFor(() => expect(calls).toContain("/api/upload/reconcile?vault_id=2"));
    expect(calls.some((url) => url.includes("/api/upload?"))).toBe(false);
    expect(screen.queryByText("essential.danger_zone")).not.toBeInTheDocument();
  });
  it("revalidates stale Cloud-only selection and never offers cloud deletion for a present local copy", async () => {
    const calls = setup("protected", true);
    await screen.findByText("exact");
    expect(screen.queryByText("essential.danger_zone")).not.toBeInTheDocument();
    expect(calls.every((url) => url.endsWith("vault_id=2"))).toBe(true);
    expect(calls.some((url) => url.startsWith("/api/cloud-purge"))).toBe(false);
  });
  it("finds an old pending deletion by exact file membership, even in a folder group outside recent50", async () => {
    const calls = setup("cloud_only", false, false, false, false, {}, true);
    await screen.findByText("essential.deletion_pending");
    expect(calls).toContain("/api/jobs?action=cloud-purge&active_only=true&path=same.jpg&vault_id=2");
    expect(screen.queryByText("essential.danger_zone")).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "essential.cancel_deletion" }));
    await waitFor(() => expect(calls).toContain("/api/jobs/cancel?vault_id=2"));
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
    expect(calls).toContain("/api/cloud-deletion/preview?vault_id=2");
    await user.type(screen.getByTestId("cloud-purge-reason"), "Deliberate removal");
    await user.type(screen.getByTestId("cloud-purge-confirmation"), "Second");
    await user.click(screen.getByTestId("cloud-purge-submit"));
    await waitFor(() => expect(calls).toContain("/api/cloud-purge?vault_id=2"));
  });
  it("shows a cloud deletion failure inside the open dialog, not in the hidden Details sheet (#651)", async () => {
    purgeFailure = { message: "Bucket refused the deletion" };
    const calls = setup("cloud_only", false);
    await screen.findByText("exact");
    const user = userEvent.setup(); await user.click(screen.getByText("essential.danger_zone"));
    await user.click(screen.getByRole("button", { name: "ui.cloud_purge_title" }));
    const dialog = await screen.findByRole("dialog", { name: "ui.cloud_purge_title" });
    await user.type(within(dialog).getByTestId("cloud-purge-reason"), "Deliberate removal");
    await user.type(within(dialog).getByTestId("cloud-purge-confirmation"), "Second");
    await user.click(within(dialog).getByTestId("cloud-purge-submit"));
    await waitFor(() => expect(calls).toContain("/api/cloud-purge?vault_id=2"));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Bucket refused the deletion");
    expect(alert).not.toHaveTextContent("ApiError");
    expect(screen.getByRole("dialog", { name: "ui.cloud_purge_title" })).toBeInTheDocument();
    expect(within(dialog).getByTestId("cloud-purge-submit")).toHaveTextContent("ui.cloud_purge_submit");
    expect(within(dialog).getByTestId("cloud-purge-submit")).toBeEnabled();
  });
});
