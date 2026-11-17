import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import type { Overview } from "@/api/vault";
import { BillingExportSection } from "@/pages/general/BillingExportSection";
import { OverviewPage } from "@/pages/overview/OverviewPage";
import { createVaultAccessFetch, defaultBillingExport, defaultVaultBilledCost, jsonResponse, renderVaultAccess } from "./vault-settings-harness";

afterEach(() => { cleanup(); resetApiClientForTests(); document.documentElement.lang = ""; });

const URL = "/api/vaults/1/billed-cost";
const plain = (text: string | null) => (text ?? "").replace(/[\u00a0\u202f]/g, " ");
const zero = { storage: 0, requests: 0, transitions: 0, retrievals: 0, data_transfer: 0, other: 0, credits: 0 };
const read = {
  state: "ok", read_at: "2026-11-17T12:00:00+00:00", exported_at: "2026-11-17T06:12:00+00:00",
  data_through: "2026-11-17T00:00:00+00:00", currency: "USD", next_check_at: "2026-11-18T12:00:00+00:00",
};
const billed = {
  ...defaultVaultBilledCost, ...read,
  periods: [
    { period: "2026-11", total: 2.62, categories: { ...zero, storage: 2.15, requests: 0.11, transitions: 0.3, retrievals: 0.07, data_transfer: 0.09, credits: -0.1 } },
    { period: "2026-10", total: 1.5, categories: { ...zero, storage: 1.5 } },
  ],
};

/** The third block of «Spazio e costi nel cloud» (#737), not a section of its own. */
async function block() {
  const usage = (await screen.findByRole("heading", { name: /Spazio e costi nel cloud|Cloud storage and cost/, level: 2 })).closest("section")!;
  expect(usage).toHaveAttribute("id", "usage");
  return within(usage).findByTestId("cloud-usage-billed");
}

// #738: the Vault page shows what AWS billed for its bucket, with the date of
// the data, as one more block next to the estimate and the CloudWatch sizes.
describe("Billed Cost on the Vault page", () => {
  it("shows each month's bill by category with the date of the data, as a cloud usage block", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({ [`GET ${URL}`]: () => jsonResponse(billed) }, { locale: "it" }) });
    const box = await block();
    expect(box).toHaveTextContent("Fatturato da AWS");
    const november = await within(box).findByTestId("billed-cost-2026-11");
    expect(plain(november.textContent)).toContain("novembre 2026");
    // An AWS bill amount is exact, never «~», in the billing currency.
    expect(plain(within(november).getByTestId("billed-cost-total").textContent)).toBe("2,62 USD");
    expect(plain(within(november).getByTestId("billed-category-transitions").textContent)).toContain("Transizioni (lifecycle)0,30 USD");
    expect(plain(within(november).getByTestId("billed-category-credits").textContent)).toContain("Crediti e sconti-0,10 USD");
    expect(within(november).queryByTestId("billed-category-other")).toBeNull();
    expect(plain(within(box).getByTestId("billed-cost-2026-10").textContent)).toContain("ottobre 2026");
    expect(within(box).getByTestId("billed-cost-date")).toHaveTextContent(/Dati AWS fino al 17 nov 2026/);
    // The estimate is the first block of the panel, not repeated here.
    expect(screen.queryByTestId("billed-cost-estimate")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Costo fatturato", level: 2 })).toBeNull();
    expect(within(box).queryByTestId("billed-cost-stale")).toBeNull();
    expect(within(box).queryByTestId("billed-cost-setup-link")).toBeNull();
  });

  it("says when the data are not up to date", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({ [`GET ${URL}`]: () => jsonResponse({ ...billed, stale: true }) }, { locale: "it" }) });
    expect(await within(await block()).findByTestId("billed-cost-stale")).toHaveTextContent(/^Dati non aggiornati/);
  });

  it("a Vault on a shared bucket shows no figure, only the way to the installation rows", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({ [`GET ${URL}`]: () => jsonResponse({ ...billed, shared: true, vault_bucket: "paolo-archive", periods: [] }) }, { locale: "en" }) });
    const box = await block();
    expect(await within(box).findByTestId("billed-cost-shared")).toHaveTextContent("installation row on the System page");
    expect(within(box).getByTestId("billed-cost-setup-link")).toHaveAttribute("href", "/system#billing-export");
    expect(within(box).queryByTestId("billed-cost-total")).toBeNull();
  });

  it("without an export shows the state and the way to set it up", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({}, { locale: "it" }) });
    const box = await block();
    const state = await within(box).findByTestId("billing-export-state");
    expect(state).toHaveAttribute("data-state", "not_configured");
    expect(state).toHaveTextContent("Nessun export di fatturazione in s3://frostvault-system-k3x9q0ab/billing/cur/");
    expect(within(box).getByTestId("billed-cost-setup-link")).toHaveAttribute("href", "/system#billing-export");
    expect(within(box).queryByTestId("billed-cost-total")).toBeNull();
  });

  it("a broken export is a quiet status with its translated reason, not figures", async () => {
    const broken = { ...defaultVaultBilledCost, state: "error", error_code: "access_denied", error_detail: "s3:GetObject (AccessDenied)" };
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({ [`GET ${URL}`]: () => jsonResponse(broken) }, { locale: "it" }) });
    const state = await within(await block()).findByTestId("billing-export-state");
    expect(state).toHaveTextContent("Non è stato possibile leggere l’export di fatturazione in s3://frostvault-system-k3x9q0ab/billing/cur/: S3 ha rifiutato s3:GetObject (AccessDenied)");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByTestId("billed-cost-setup-link")).toHaveTextContent("Stato dell’export di fatturazione");
  });
});

function renderSystemSection(body: unknown, locale: "en" | "it" = "en") {
  resetApiClientForTests();
  configureApiClient({ fetch: createVaultAccessFetch({ "GET /api/billing-export": () => jsonResponse(body) }, { locale }) });
  render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider><BillingExportSection /></I18nProvider></ApiQueryProvider>);
}

describe("Billing export on the System page", () => {
  it("before the export: the state, the setup steps and the bucket policy for the System Bucket", async () => {
    renderSystemSection(defaultBillingExport);
    const setup = await screen.findByTestId("billing-setup");
    expect(setup).toHaveTextContent("Table: CUR 2.0. Tick «Include resource IDs»");
    expect(setup).toHaveTextContent("gzip – text/csv");
    expect(setup).toHaveTextContent("select the existing bucket frostvault-system-k3x9q0ab");
    // Only the columns FrostVault reads, and a warning that the console replaces a bucket policy.
    expect(setup).toHaveTextContent("select only the ones FrostVault reads");
    expect(setup).toHaveTextContent("line_item_net_unblended_cost");
    expect(setup).toHaveTextContent("line_item_line_item_type");
    expect(setup).toHaveTextContent("The console replaces any bucket policy the bucket already has.");
    expect(setup).toHaveTextContent("S3 path prefix: billing/cur");
    const policy = JSON.parse(screen.getByTestId("billing-policy").textContent ?? "{}");
    expect(policy.Statement[0].Principal.Service).toEqual(["bcm-data-exports.amazonaws.com"]);
    expect(policy.Statement[0].Resource).toBe("arn:aws:s3:::frostvault-system-k3x9q0ab/*");
    expect(screen.getByTestId("billing-export-state")).toHaveAttribute("data-state", "not_configured");
  });

  it("once read: every Vault and the installation rows per month", async () => {
    renderSystemSection({
      ...defaultBillingExport, ...read,
      periods: [{
        period: "2026-11", total: 7.835, vaults_total: 2.2,
        vaults: [{ vault_id: 2, name: "Documenti", bucket: "frostvault-documenti-x9y8z7w6", total: 2.2, categories: { ...zero, storage: 2, requests: 0.2 } }],
        installation: [
          { role: "shared", bucket: "paolo-archive", total: 3, categories: { ...zero, storage: 3 } },
          { role: "system", bucket: "frostvault-system-k3x9q0ab", total: 0.015, categories: { ...zero, storage: 0.01, requests: 0.005 } },
        ],
      }],
    });
    const period = await screen.findByTestId("billing-period-2026-11");
    expect(plain(period.textContent)).toContain("Total $7.84");
    const rows = within(period).getAllByTestId("billing-row").map((row) => plain(row.textContent));
    expect(rows[0]).toContain("Documenti$2.20");
    expect(rows[1]).toContain("Installation · shared bucket of older Vaults paolo-archive$3.00");
    expect(rows[2]).toContain("Installation · System Bucket frostvault-system-k3x9q0ab$0.02");
    expect(within(period).getByRole("link", { name: "Documenti" })).toHaveAttribute("href", "/vaults/2/settings#usage");
    expect(screen.queryByTestId("billing-export-state")).toBeNull();
    expect(screen.getByTestId("billed-cost-date")).toHaveTextContent(/AWS data up to/);
  });
});

describe("Billed Cost on the Overview", () => {
  const overview = { vaults: [{ id: 1, uuid: "u1", name: "Foto", lifecycle: "active", monthly_estimate: 1.2, open_problem_count: 0 }],
    open_problem_count: 0, month: "2026-11", monthly_estimate: 1.2, previous_month_delta: null, currency: "EUR" } as unknown as Overview;
  let billingAnswered = false;
  function renderOverview(body: unknown) {
    resetApiClientForTests();
    billingAnswered = false;
    configureApiClient({ fetch: createVaultAccessFetch({
      "GET /api/billing-export": () => { billingAnswered = true; return jsonResponse(body); },
      "GET /api/general/vaults": () => jsonResponse({ items: [] }),
      "GET /api/stats": () => jsonResponse({ states: { protected: 1 }, storage: {}, runtime: {} }),
    }, { locale: "en" }) });
    render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider>
      <OverviewPage vaults={[{ id: 1, uuid: "u1", name: "Foto" } as never]} overview={overview} /></I18nProvider></ApiQueryProvider>);
  }

  it("names what AWS billed for the FrostVault buckets in the newest month, not the whole bill", async () => {
    renderOverview({ ...defaultBillingExport, ...read, periods: [{ period: "2026-11", total: 7.835, vaults_total: 4.82, vaults: [], installation: [] }] });
    expect(await screen.findByTestId("tile-cost-billed")).toHaveTextContent("Billed by AWS · FrostVault buckets · November 2026: $4.82");
  });

  it("shows no setup link: the steps live on the System page", async () => {
    renderOverview(defaultBillingExport);
    await waitFor(() => expect(billingAnswered).toBe(true));
    expect(await screen.findByTestId("tile-cost")).toBeInTheDocument();
    expect(screen.queryByTestId("tile-cost-billed")).toBeNull();
    expect(screen.queryByTestId("tile-cost-setup")).toBeNull();
  });
});
