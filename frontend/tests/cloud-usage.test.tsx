import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";
import { createVaultAccessFetch, defaultCloudUsage, jsonResponse, loadCatalog, renderVaultAccess } from "./vault-settings-harness";

afterEach(() => { cleanup(); resetApiClientForTests(); document.documentElement.lang = ""; });

const URL = "/api/vaults/1/cloud-usage";
const GIB = 1024 ** 3;
const KIB = 1024;
const plain = (text: string | null) => (text ?? "").replace(/[\u00a0\u202f]/g, " ");

const estimate = {
  versions: 141733, bytes: 539 * GIB, monthly_cost: 1.23, currency: "EUR", region: "eu-south-1", pricing_effective_at: "2026-10-01",
  by_storage_class: [
    { storage_class: "STANDARD", versions: 86700, bytes: 316 * GIB, monthly_cost: 0.9 },
    { storage_class: "DEEP_ARCHIVE", versions: 55033, bytes: 223 * GIB, monthly_cost: 0.33 },
  ],
};

const measured = {
  status: "available",
  day: "2026-11-15",
  read_at: "2026-11-16T03:00:00+00:00",
  object_count: 141950,
  bytes: 540 * GIB,
  overhead_bytes: 40 * 55033 * KIB,
  last_read: { attempted_at: "2026-11-16T03:00:00+00:00", outcome: "ok", error_code: null },
  next_read_after: "2026-11-17T03:00:00+00:00",
  by_storage_class: [
    { storage_class: "STANDARD", bytes: 317 * GIB, overhead_bytes: 0, overhead: [] },
    { storage_class: "DEEP_ARCHIVE", bytes: 223 * GIB, overhead_bytes: 40 * 55033 * KIB, overhead: [
      { storage_type: "DeepArchiveObjectOverhead", kind: "object_overhead", billed_as: "DEEP_ARCHIVE", bytes: 32 * 55033 * KIB },
      { storage_type: "DeepArchiveS3ObjectOverhead", kind: "object_overhead", billed_as: "STANDARD", bytes: 8 * 55033 * KIB },
    ] },
  ],
  history: [
    { day: "2026-11-15", bytes: 540 * GIB, overhead_bytes: 40 * 55033 * KIB, object_count: 141950 },
    { day: "2026-11-14", bytes: 538 * GIB, overhead_bytes: 40 * 55000 * KIB, object_count: 141000 },
  ],
};

const usage = (overrides: Record<string, unknown>) => ({ ...defaultCloudUsage, bucket: "frostvault-docs-ab12cd34", dedicated_bucket: true, estimate, ...overrides });

function show(body: unknown, locale: "en" | "it" = "it") {
  renderVaultAccess({ fetchImpl: createVaultAccessFetch({ [`GET ${URL}`]: () => jsonResponse(body) }, { locale }) });
}

async function section(name: string) {
  return (await screen.findByRole("heading", { name, level: 2 })).closest("section")!;
}

// #737: the Vault page shows the catalog estimate next to the bytes and
// objects CloudWatch measured, with the date of the data point.
describe("Cloud usage on the Vault page", () => {
  it("shows the catalog estimate next to the CloudWatch sizes, with grouped thousands and the data date", async () => {
    show(usage({ measured }));
    const usageSection = await section("Spazio e costi nel cloud");
    expect(usageSection).toHaveAttribute("id", "usage");
    const row = await within(usageSection).findByTestId("cloud-usage-estimate-DEEP_ARCHIVE");
    expect(plain(row.textContent)).toContain("Deep Archive");
    expect(plain(row.textContent)).toContain("55.033");
    expect(plain(row.textContent)).toContain("223,0 GiB");
    expect(plain(row.textContent)).toContain("~0,33 €");
    expect(plain(within(usageSection).getByTestId("cloud-usage-estimate-total").textContent)).toContain("141.733");
    expect(within(usageSection).getByTestId("cloud-usage-estimate-disclaimer")).toHaveTextContent(/Stima dai listini pubblici AWS \(eu-south-1/);

    expect(plain(within(usageSection).getByTestId("cloud-usage-measured-day").textContent)).toMatch(/^Dati del 15 nov 2026 \(UTC\), letti il /);
    const measuredRow = within(usageSection).getByTestId("cloud-usage-measured-DEEP_ARCHIVE");
    expect(plain(measuredRow.textContent)).toContain("223,0 GiB");
    // The per-object overhead is shown apart, with the class it is billed in.
    expect(plain(measuredRow.textContent)).toContain("2,1 GiB");
    expect(plain(measuredRow.textContent)).toContain("metadati per oggetto · fatturati come Standard");
    expect(plain(within(usageSection).getByTestId("cloud-usage-measured-objects").textContent)).toBe("141.950 oggetti nel bucket.");
    expect(within(usageSection).getByTestId("cloud-usage-measured-lag")).toHaveTextContent(/circa un giorno di ritardo/);
    expect(within(usageSection).getByTestId("cloud-usage-overhead-help")).toHaveTextContent(/32 KB di indice/);
    expect(within(usageSection).getByTestId("cloud-usage-history")).toBeInTheDocument();
    expect(within(usageSection).queryByTestId("cloud-usage-measured-note")).toBeNull();
    // Room for a third block (the Billed Cost, #738) on wide screens.
    expect(within(usageSection).getByTestId("cloud-usage")).toHaveClass("lg:grid-cols-2", "xl:grid-cols-3");
  });

  it("a Vault on a shared bucket shows only the catalog estimate, with a note", async () => {
    show(usage({ bucket: "shared-archive", dedicated_bucket: false, measured: { status: "shared_bucket" } }), "en");
    const usageSection = await section("Cloud storage and cost");
    expect(await within(usageSection).findByTestId("cloud-usage-measured-note")).toHaveTextContent(/shared with other Vaults/);
    expect(within(usageSection).getByTestId("cloud-usage-estimate-STANDARD")).toBeInTheDocument();
    expect(within(usageSection).queryByTestId("cloud-usage-measured-day")).toBeNull();
    expect(within(usageSection).queryByTestId("cloud-usage-measured-lag")).toBeNull();
  });

  it("without cloudwatch:GetMetricStatistics shows a note, not an error", async () => {
    show(usage({ measured: { status: "access_denied", history: [], next_read_after: "2026-11-16T09:00:00+00:00",
      last_read: { attempted_at: "2026-11-16T03:00:00+00:00", outcome: "access_denied", error_code: "AccessDenied" } } }), "en");
    const usageSection = await section("Cloud storage and cost");
    const note = await within(usageSection).findByTestId("cloud-usage-measured-note");
    expect(note).toHaveTextContent(/cloudwatch:GetMetricStatistics/);
    expect(note).toHaveAttribute("role", "note");
    expect(within(usageSection).queryByRole("alert")).toBeNull();
  });

  it("keeps the last data after a failed read and says so", async () => {
    show(usage({ measured: { ...measured, last_read: { attempted_at: "2026-11-17T03:00:00+00:00", outcome: "failed", error_code: "Throttling" } } }), "en");
    const note = await screen.findByTestId("cloud-usage-measured-note");
    expect(note).toHaveTextContent("The last read from CloudWatch failed (Throttling). FrostVault tries again within 6 hours.");
    expect(note).toHaveTextContent("The data shown come from the last successful read.");
    expect(screen.getByTestId("cloud-usage-measured-day")).toBeInTheDocument();
  });

  it("says when AWS has published nothing yet and when no read ran", async () => {
    show(usage({ measured: { status: "no_data", history: [], next_read_after: null, last_read: { attempted_at: "2026-11-16T03:00:00+00:00", outcome: "no_data", error_code: null } } }), "en");
    expect(await screen.findByTestId("cloud-usage-measured-note")).toHaveTextContent(/after a day or two/);
    cleanup();
    show(usage({ measured: { status: "pending", history: [], next_read_after: null, last_read: null } }), "en");
    expect(await screen.findByTestId("cloud-usage-measured-note")).toHaveTextContent(/has not run yet/);
  });

  it("names every message in both languages", () => {
    for (const locale of ["en", "it"] as const) {
      const messages = loadCatalog(locale);
      for (const key of ["title", "summary", "estimate_title", "estimate_help", "estimate_none", "estimate_disclaimer",
        "measured_title", "measured_help", "measured_day", "measured_objects_one", "measured_objects_other", "measured_objects_unknown",
        "measured_lag", "overhead_help", "overhead_object", "overhead_size", "overhead_staging", "kind.object_overhead",
        "kind.size_overhead", "kind.staging", "billed_as", "shared_bucket", "provisioning", "pending", "no_data",
        "access_denied", "failed", "stale", "history_title_one", "history_title_other", "next_read"]) {
        expect(messages[`cloud_usage.${key}`], `${locale} ${key}`).toBeTruthy();
      }
      expect(messages["storage.REDUCED_REDUNDANCY"]).toBeTruthy();
    }
  });
});
