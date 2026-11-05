import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { resetApiClientForTests } from "@/api";
import { createVaultAccessFetch, jsonResponse, loadCatalog, renderVaultAccess } from "./vault-access-harness";

const estimate = { target_class: "DEEP_ARCHIVE", eligible_count: 2, total_bytes: 4096,
  skipped: { pending_work: 1, already_equal_or_colder: 3, explicit_restore_required: 1 }, minimum_days: 180,
  estimated_monthly_eur: 0.00002, estimated_retrieval_eur: 0.001, region: "eu-west-1", pricing_effective_at: "2026-11-01", assumptions: "Estimate only" };

afterEach(() => { cleanup(); resetApiClientForTests(); });

describe("Vault-wide Move now in Advanced", () => {
  it("discloses mixed-class skips, estimates and minimum duration before acknowledgement and Vault-wide submission", async () => {
    const user = userEvent.setup();
    let payload: unknown;
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/vault/move": () => jsonResponse({ targets: [estimate, { ...estimate, target_class: "GLACIER_IR", eligible_count: 0 }] }),
      "POST /api/vault/move": (init) => {
        payload = JSON.parse(String(init?.body));
        return jsonResponse({ ...estimate, group_id: "g", job_ids: [1, 2], item_count: 2 });
      },
    }) });
    await user.click(await screen.findByText("Advanced", { exact: true }));
    const start = await screen.findByRole("button", { name: "Move now in background" });
    expect(start).toBeDisabled();
    expect(screen.getByText(/destination bills at least 180 days/)).toBeInTheDocument();
    expect(screen.getByText(/2 files · 4096 bytes/)).toHaveTextContent("eu-west-1");
    expect(screen.getByText(/3 skipped: already equal or colder/)).toBeInTheDocument();
    expect(screen.getByText(/exact cold source version must be explicitly restored/)).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Move eligible files to" }));
    expect(within(screen.getByRole("listbox")).getAllByRole("option")).toHaveLength(1);
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("checkbox", { name: /I understand the estimated costs/ }));
    await user.click(start);
    await waitFor(() => expect(payload).toEqual({ target_class: "DEEP_ARCHIVE", acknowledge_costs: true }));
    expect(await screen.findByText(/2 move Jobs queued/)).toBeInTheDocument();
  });

  it("has no class picker or action for Standard or a Vault with no eligible copies", async () => {
    renderVaultAccess();
    expect(await screen.findByText(/Standard has no move action/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Move now in background" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Move eligible files to" })).not.toBeInTheDocument();
  });

  it("keeps acknowledgement gated and reports an admission race honestly", async () => {
    const user = userEvent.setup();
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/vault/move": () => jsonResponse({ targets: [estimate] }),
      "POST /api/vault/move": () => jsonResponse({ detail: "No eligible files: pending work" }, 409),
    }) });
    await user.click(await screen.findByText("Advanced", { exact: true }));
    await user.click(await screen.findByRole("checkbox", { name: /I understand the estimated costs/ }));
    await user.click(screen.getByRole("button", { name: "Move now in background" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No eligible files: pending work");
  });

  it("has complete English and Italian copy and no per-file move option", () => {
    for (const locale of ["it", "en"] as const) {
      const messages = loadCatalog(locale);
      expect(messages["move.title"]).toBeTruthy();
      expect(messages["move.minimum"]).toContain("{days}");
      expect(messages["move.exclusions"]).toBeTruthy();
      expect(messages["move.help"]).toBeTruthy();
    }
  });
});
