import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { configureApiClient, resetApiClientForTests } from "@/api";
import { RecoverSelection, type Target } from "@/pages/archive/RecoverSelection";
import { jsonResponse } from "./vault-settings-harness";
const target: Target = { vaultId: 2, vaultName: "Second", item: { type: "file", name: "same.jpg", path: "same.jpg", state: "cloud_only" } };
const t = (key: string, params?: Record<string, string | number>) => `${key}${params ? JSON.stringify(params) : ""}`;
afterEach(resetApiClientForTests);
describe("essential recovery", () => {
  it("defaults cold recovery to Bulk, estimates Standard on request, pins return path and full local retention", async () => {
    const bodies: { url: string; body: Record<string, unknown> }[] = [];
    configureApiClient({ fetch: vi.fn(async (input, init) => {
      const url = String(input); const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      if (init?.method === "POST") bodies.push({ url, body });
      if (url.startsWith("/api/vault/operation-policy")) return jsonResponse({ local_release_after_days: 180 });
      if (url.startsWith("/api/recover/estimate")) return jsonResponse({ archive_version_id: "exact-snapshot", requires_restore: true, estimate: { estimated_cost_eur: 1, estimated_hours: 48, tier: body.restore_tier, days: 3 } });
      return jsonResponse({});
    }) });
    const done = vi.fn(); render(<RecoverSelection targets={[target]} t={t} onClose={vi.fn()} onDone={done} />);
    expect(await screen.findByRole("combobox")).toHaveValue("Bulk");
    expect(screen.getByText(/essential.return_path/)).toHaveTextContent("Second / same.jpg");
    expect(screen.getByText(/essential.recovered_retention/)).toHaveTextContent("180");
    const user = userEvent.setup(); await user.selectOptions(screen.getByRole("combobox"), "Standard");
    await waitFor(() => expect(screen.getByRole("button", { name: "ui.row_action_recover" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "ui.row_action_recover" }));
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(bodies.at(-1)).toEqual({ url: "/api/recover?vault_id=2", body: { path: "same.jpg", is_directory: false, restore_tier: "Standard", archive_version_id: "exact-snapshot", restore_days: 3 } });
  });
  it("hides cold tier choices for immediate recovery", async () => {
    configureApiClient({ fetch: vi.fn(async (url) => String(url).includes("estimate") ? jsonResponse({ requires_restore: false }) : jsonResponse({ local_release_after_days: null })) });
    render(<RecoverSelection targets={[target]} t={t} onClose={vi.fn()} onDone={vi.fn()} />);
    await screen.findByText("essential.ready_now"); expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });
  it("keeps many items in a bounded, scrolling body with the actions always outside it (#522)", async () => {
    configureApiClient({ fetch: vi.fn(async (url) => String(url).includes("estimate") ? jsonResponse({ requires_restore: true, estimate: { estimated_cost_eur: 0.5, estimated_hours: 12, days: 3 } }) : jsonResponse({ local_release_after_days: null })) });
    const many = Array.from({ length: 7 }, (_, index) => ({ ...target, item: { ...target.item, name: `f${index}.jpg`, path: `f${index}.jpg` } }));
    render(<RecoverSelection targets={many} t={t} onClose={vi.fn()} onDone={vi.fn()} />);
    expect(await screen.findByTestId("recover-summary")).toHaveTextContent('essential.recover_summary_cold{"count":7,"cost":"3.50","hours":12}');
    const dialog = screen.getByRole("dialog");
    expect(dialog.className).toMatch(/max-h-\[calc\(100dvh/);
    const body = screen.getByTestId("dialog-body");
    expect(body.className).toContain("overflow-y-auto");
    const recover = screen.getByRole("button", { name: "ui.row_action_recover" });
    expect(body).not.toContainElement(recover);
    expect(body).not.toContainElement(screen.getByRole("button", { name: "ui.cancel" }));
    expect(screen.getByText("essential.recover_items").closest("details")).not.toHaveAttribute("open");
  });
});
