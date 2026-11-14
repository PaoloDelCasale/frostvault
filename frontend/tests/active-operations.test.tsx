import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import { ActiveOperations } from "@/components/ActiveOperations";
import { jsonResponse } from "./vault-settings-harness";
afterEach(resetApiClientForTests);
describe("active Owner work without a routine notification feed", () => {
  it("preserves high-impact approval and cancellation in immutable Vault scope, excluding routine uploads", async () => {
    const calls: string[] = [];
    configureApiClient({ fetch: vi.fn(async (input) => {
      const url = String(input); calls.push(url);
      if (url.startsWith("/api/jobs?")) return jsonResponse({ groups: [
        { id: "restore", action: "recover", path: "photos", status: "pending_approval", item_count: 2, completed_count: 0, percent: 0, estimated_cost_eur: 20, estimated_hours: 48 },
        { id: "upload", action: "upload", path: "routine.jpg", status: "uploading", item_count: 1, completed_count: 0, percent: 5 },
      ] });
      return jsonResponse({});
    }) });
    render(<ApiQueryProvider client={createAppQueryClient()}><ActiveOperations vaultId={2} t={(key, params) => `${key}${params ? JSON.stringify(params) : ""}`} /></ApiQueryProvider>);
    const user = userEvent.setup(); await user.click(await screen.findByRole("button", { name: "ui.approve_restore" }));
    expect(screen.getByText(/essential.approval_cost/)).toHaveTextContent('{"cost":"20.00","hours":48}');
    await waitFor(() => expect(calls).toContain("/api/recover/approve?vault_id=2"));
    await user.click(screen.getByRole("button", { name: "ui.cancel" }));
    await waitFor(() => expect(calls).toContain("/api/jobs/cancel?vault_id=2"));
    expect(screen.queryByText(/routine.jpg/)).not.toBeInTheDocument();
    expect(calls).toContain("/api/jobs?action=recover&action=free-space&active_only=true&vault_id=2");
  });
  it("pages bounded active summaries instead of hiding older approvals behind unrelated jobs", async () => {
    const calls: string[] = [];
    configureApiClient({ fetch: vi.fn(async (input) => {
      const url = String(input); calls.push(url);
      const older = url.includes("groups_before_id=100");
      return jsonResponse({ groups: [{ id: older ? "older" : "newer", action: "recover", path: older ? "old-folder" : "new-folder", status: "pending_approval", item_count: 30001, completed_count: 0, percent: 0 }], next_groups_before_id: older ? null : 100 });
    }) });
    render(<ApiQueryProvider client={createAppQueryClient()}><ActiveOperations vaultId={2} t={(key) => key} /></ApiQueryProvider>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "ui.next" }));
    await screen.findByText("action.recover · old-folder");
    expect(calls).toContain("/api/jobs?action=recover&action=free-space&active_only=true&groups_before_id=100&vault_id=2");
    expect(screen.getByText(/0\/30001/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "ui.next" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "ui.previous" }));
    await screen.findByText("action.recover · new-folder");
  });
  it("keeps Move and recovery read scopes separate in the same Vault cache", async () => {
    const calls: string[] = [];
    configureApiClient({ fetch: vi.fn(async (input) => {
      const url = String(input); calls.push(url);
      const move = url.includes("action=storage-class");
      return jsonResponse({ groups: [{ id: move ? "move" : "restore", action: move ? "storage-class" : "recover", path: move ? "move-folder" : "restore-folder", status: "queued", item_count: 1, completed_count: 0, percent: 0 }] });
    }) });
    render(<ApiQueryProvider client={createAppQueryClient()}><ActiveOperations vaultId={2} t={(key) => key} /><ActiveOperations vaultId={2} moves t={(key) => key} /></ApiQueryProvider>);
    await screen.findByText("move.title · move-folder");
    await screen.findByText("action.recover · restore-folder");
    expect(calls).toContain("/api/jobs?action=storage-class&active_only=true&vault_id=2");
  });
  it("shows a cloud restore as a wait with times, and stopping it explains that AWS continues (#646)", async () => {
    const calls: string[] = [];
    configureApiClient({ fetch: vi.fn(async (input) => {
      const url = String(input); calls.push(url);
      if (url.startsWith("/api/jobs?")) return jsonResponse({ groups: [
        { id: "cold", action: "recover", path: "cold/c0.bin", status: "restoring", item_count: 1, completed_count: 0, percent: 0, restore_tier: "Standard", restore_days: 1, requested_at: "2026-11-14T09:57:00Z", ready_by: "2026-11-14T21:57:00Z" },
      ] });
      return jsonResponse({});
    }) });
    render(<ApiQueryProvider client={createAppQueryClient()}><ActiveOperations vaultId={2} t={(key, params) => `${key}${params ? JSON.stringify(params) : ""}`} /></ApiQueryProvider>);
    const line = await screen.findByText(/^operation\.restoring/);
    expect(line).toHaveTextContent(/recovery\.requested_at\{"time":".+"\} · recovery\.tier\.Standard · recovery\.ready_by\{"time":".+"\}/);
    expect(line).not.toHaveTextContent("%");
    expect(screen.queryByRole("button", { name: "ui.cancel" })).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "recovery.stop_waiting" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent('recovery.stop_waiting_help_days{"days":1}');
    await user.click(within(dialog).getByRole("button", { name: "recovery.keep_waiting" }));
    expect(calls).not.toContain("/api/jobs/cancel?vault_id=2");
    await user.click(screen.getByRole("button", { name: "recovery.stop_waiting" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "recovery.stop_waiting" }));
    await waitFor(() => expect(calls).toContain("/api/jobs/cancel?vault_id=2"));
    expect(await screen.findByRole("status")).toHaveTextContent("ui.operation_not_cancellable");
  });
  it("cancels a recovery AWS has not accepted yet without a dialog or a not-cancellable notice", async () => {
    const calls: string[] = [];
    configureApiClient({ fetch: vi.fn(async (input) => {
      const url = String(input); calls.push(url);
      if (url.startsWith("/api/jobs?")) return jsonResponse({ groups: [{ id: "queued", action: "recover", path: "cold/c1.bin", status: "queued", item_count: 1, completed_count: 0, percent: 0 }] });
      return jsonResponse({});
    }) });
    render(<ApiQueryProvider client={createAppQueryClient()}><ActiveOperations vaultId={2} t={(key) => key} /></ApiQueryProvider>);
    await userEvent.setup().click(await screen.findByRole("button", { name: "ui.cancel" }));
    await waitFor(() => expect(calls).toContain("/api/jobs/cancel?vault_id=2"));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.queryByText("ui.operation_not_cancellable")).not.toBeInTheDocument();
  });
});
