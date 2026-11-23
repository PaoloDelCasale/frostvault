import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { resetApiClientForTests } from "@/api";
import { createVaultAccessFetch, loadCatalog, renderVaultAccess } from "./vault-settings-harness";
import { jsonResponse } from "./support/http";

const estimate = { target_class: "DEEP_ARCHIVE", eligible_count: 2, total_bytes: 4096,
  skipped: { pending_work: 1, already_equal_or_colder: 3, explicit_restore_required: 1 }, minimum_days: 180,
  estimated_monthly_eur: 0.00002, estimated_retrieval_eur: 0.001, region: "eu-west-1", pricing_effective_at: "2026-11-01", assumptions: "Estimate only" };

afterEach(() => { cleanup(); resetApiClientForTests(); });

describe("Vault-wide Move now in the storage plan section", () => {
  it("discloses mixed-class skips, estimates and minimum duration before acknowledgement and Vault-wide submission", async () => {
    const user = userEvent.setup();
    let payload: unknown;
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/vaults/1/move": () => jsonResponse({ vault_id: 1, targets: [estimate, { ...estimate, target_class: "GLACIER_IR", eligible_count: 0 }] }),
      "POST /api/vaults/1/move": (init) => {
        payload = JSON.parse(String(init?.body));
        return jsonResponse({ ...estimate, group_id: "g", job_ids: [1, 2], item_count: 2 });
      },
    }) });
    await user.click(await screen.findByText("Show plan and moves", { exact: true }));
    const start = await screen.findByRole("button", { name: "Move now in background" });
    expect(start).toBeDisabled();
    expect(screen.getByText(/destination bills at least 180 days/)).toBeInTheDocument();
    const summary = screen.getByTestId("vault-move-estimate");
    expect(within(summary).getByText("< €0.01/month").tagName).toBe("STRONG");
    expect(summary).toHaveTextContent("storage for 2 files (4.0 KiB)");
    expect(summary).toHaveTextContent("Source retrieval < €0.01. AWS list prices for eu-west-1, published Nov 1, 2026");
    expect(screen.getByText(/3 skipped: already equal or colder/)).toBeInTheDocument();
    expect(screen.getByText(/exact cold source version must be explicitly recovered/)).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Move eligible files to" }));
    expect(within(screen.getByRole("listbox")).getAllByRole("option")).toHaveLength(1);
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("checkbox", { name: /I understand the estimated costs/ }));
    await user.click(start);
    await waitFor(() => expect(payload).toEqual({ target_class: "DEEP_ARCHIVE", acknowledge_costs: true }));
    expect(await screen.findByText(/2 move Jobs queued/)).toBeInTheDocument();
  });

  it("leads with the formatted monthly cost and keeps pricing details on a secondary line", async () => {
    const user = userEvent.setup();
    const ia = { ...estimate, target_class: "STANDARD_IA", eligible_count: 593, total_bytes: 22172592158,
      estimated_monthly_eur: 0.270513, estimated_retrieval_eur: 0, pricing_effective_at: "2026-10-15T17:35:40Z" };
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/vaults/1/move": () => jsonResponse({ vault_id: 1, targets: [ia] }),
    }) });
    await user.click(await screen.findByText("Show plan and moves", { exact: true }));
    const summary = await screen.findByTestId("vault-move-estimate");
    expect(within(summary).getByText("≈ €0.27/month").tagName).toBe("STRONG");
    expect(summary).toHaveTextContent("storage for 593 files (20.6 GiB)");
    expect(summary).toHaveTextContent("Source retrieval €0.00. AWS list prices for eu-west-1, published Oct 15, 2026 (USD used 1:1 as EUR).");
    expect(summary).not.toHaveTextContent("22172592158");
    expect(summary).not.toHaveTextContent("0.270513");
    expect(summary).not.toHaveTextContent("2026-10-15T");
  });

  it("formats the estimate for the Italian locale", async () => {
    const user = userEvent.setup();
    const ia = { ...estimate, target_class: "STANDARD_IA", eligible_count: 12593, total_bytes: 22172592158,
      estimated_monthly_eur: 0.270513, estimated_retrieval_eur: 0.004, pricing_effective_at: "2026-10-15T12:00:00Z" };
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/vaults/1/move": () => jsonResponse({ vault_id: 1, targets: [ia] }),
    }, { locale: "it" }) });
    await user.click(await screen.findByText(loadCatalog("it")["settings.plan_open"], { exact: true }));
    const summary = await screen.findByTestId("vault-move-estimate");
    expect(within(summary).getByText(/^≈ 0,27\s€\/mese$/).tagName).toBe("STRONG");
    expect(summary).toHaveTextContent(/di archiviazione per 12\.593 file \(20,6 GiB\)/);
    expect(summary).toHaveTextContent(/Lettura dalla sorgente < 0,01\s€\./);
    expect(summary).toHaveTextContent("pubblicato il 15 ott 2026");
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
      "GET /api/vaults/1/move": () => jsonResponse({ vault_id: 1, targets: [estimate] }),
      "POST /api/vaults/1/move": () => jsonResponse({ detail: "No eligible files: pending work" }, 409),
    }) });
    await user.click(await screen.findByText("Show plan and moves", { exact: true }));
    await user.click(await screen.findByRole("checkbox", { name: /I understand the estimated costs/ }));
    await user.click(screen.getByRole("button", { name: "Move now in background" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No eligible files: pending work");
  });

  it("reports an unavailable preview inline and keeps the rest of the plan usable (#718)", async () => {
    const user = userEvent.setup();
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/vaults/1/move": () => jsonResponse({ detail: "Internal Server Error" }, 500),
    }) });
    await user.click(await screen.findByText("Show plan and moves", { exact: true }));
    const notice = await screen.findByTestId("vault-move-preview-unavailable");
    expect(notice).toHaveTextContent(/^Move preview unavailable\. /);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: loadCatalog("en")["move.title"] })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Move now in background" })).not.toBeInTheDocument();
  });

  it("keeps queued Jobs reported when only the refreshed preview fails", async () => {
    const user = userEvent.setup();
    let previews = 0;
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/vaults/1/move": () => (previews++ === 0
        ? jsonResponse({ vault_id: 1, targets: [estimate] })
        : jsonResponse({ detail: "Internal Server Error" }, 500)),
      "POST /api/vaults/1/move": () => jsonResponse({ ...estimate, group_id: "g", job_ids: [1, 2], item_count: 2 }),
    }) });
    await user.click(await screen.findByText("Show plan and moves", { exact: true }));
    await user.click(await screen.findByRole("checkbox", { name: /I understand the estimated costs/ }));
    await user.click(screen.getByRole("button", { name: "Move now in background" }));
    expect(await screen.findByText(/2 move Jobs queued/)).toBeInTheDocument();
    expect(await screen.findByTestId("vault-move-preview-unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("has complete English and Italian copy and no per-file move option", () => {
    for (const locale of ["it", "en"] as const) {
      const messages = loadCatalog(locale);
      expect(messages["move.title"]).toBeTruthy();
      expect(messages["move.minimum"]).toContain("{days}");
      expect(messages["move.exclusions"]).toBeTruthy();
      expect(messages["move.help"]).toBeTruthy();
      expect(messages["move.preview_unavailable"]).toContain("{detail}");
    }
  });
});
