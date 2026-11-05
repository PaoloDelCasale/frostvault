import { render, screen, waitFor } from "@testing-library/react";
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
    render(<ApiQueryProvider client={createAppQueryClient()}><ActiveOperations vaultId={2} t={(key) => key} /></ApiQueryProvider>);
    const user = userEvent.setup(); await user.click(await screen.findByRole("button", { name: "ui.approve_restore" }));
    await waitFor(() => expect(calls).toContain("/api/recover/approve?vault_id=2"));
    await user.click(screen.getByRole("button", { name: "ui.cancel" }));
    await waitFor(() => expect(calls).toContain("/api/jobs/cancel?vault_id=2"));
    expect(screen.queryByText(/routine.jpg/)).not.toBeInTheDocument();
  });
});
