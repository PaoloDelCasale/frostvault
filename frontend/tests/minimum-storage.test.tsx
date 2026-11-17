import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";
import { createVaultAccessFetch, defaultMinimumStorage, jsonResponse, loadCatalog, renderVaultAccess } from "./vault-settings-harness";

afterEach(() => { cleanup(); resetApiClientForTests(); document.documentElement.lang = ""; });

const URL = "/api/vaults/1/minimum-storage";
const GIB = 1024 ** 3;
const plain = (text: string | null) => (text ?? "").replace(/[\u00a0\u202f]/g, " ");

const summary = {
  ...defaultMinimumStorage,
  by_storage_class: [
    { storage_class: "DEEP_ARCHIVE", minimum_days: 180, within: { count: 55033, bytes: 223 * GIB }, beyond: { count: 0, bytes: 0 }, remaining_cost: 0.3825, estimated_count: 55033 },
    { storage_class: "STANDARD", minimum_days: 0, within: { count: 0, bytes: 0 }, beyond: { count: 86700, bytes: 316 * GIB }, remaining_cost: 0, estimated_count: 0 },
  ],
  remaining_cost: 0.3825,
  releases: [{ month: "2027-05", count: 55033, bytes: 223 * GIB }],
  estimated_count: 55033,
};

// #731: the Vault page says, per class, what is within and beyond the minimum,
// what it still bills and when it is released, as a list-price estimate.
describe("Minimum storage duration on the Vault page", () => {
  it("shows per class within and beyond, the remaining minimum cost and the month-by-month release", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({ [`GET ${URL}`]: () => jsonResponse(summary) }, { locale: "it" }) });
    const section = (await screen.findByRole("heading", { name: "Durata minima di archiviazione", level: 2 })).closest("section")!;
    expect(section).toHaveAttribute("id", "minimum");
    const row = await within(section).findByTestId("minimum-storage-DEEP_ARCHIVE");
    expect(plain(row.textContent)).toContain("Deep Archive");
    expect(plain(row.textContent)).toContain("180 giorni");
    expect(plain(row.textContent)).toContain("55.033 versioni · 223,0 GiB");
    expect(plain(row.textContent)).toContain("~0,38 €");
    // A class without a minimum is not a row.
    expect(within(section).queryByTestId("minimum-storage-STANDARD")).toBeNull();
    expect(plain(within(section).getByTestId("minimum-storage-cost").textContent)).toContain("Costo minimo ancora da pagare: ~0,38 €.");
    expect(plain(within(section).getByTestId("minimum-storage-releases").textContent)).toContain("maggio 2027: 55.033 versioni · 223,0 GiB");
    expect(within(section).getByTestId("minimum-storage-estimated")).toHaveTextContent("Per 55.033 versioni la data di ingresso nella classe è stimata dal piano di archiviazione.");
    expect(within(section).getByTestId("minimum-storage-disclaimer")).toHaveTextContent(/Stime dai listini pubblici AWS \(eu-south-1/);
  });

  it("says when no version is in a class with a minimum, still as an estimate", async () => {
    renderVaultAccess();
    expect(await screen.findByTestId("minimum-storage-none")).toHaveTextContent("No version is in a class with a minimum duration.");
    expect(screen.getByTestId("minimum-storage-disclaimer")).toHaveTextContent(/not from your AWS bill/);
  });

  it("names every message in both languages", () => {
    for (const locale of ["en", "it"] as const) {
      const messages = loadCatalog(locale);
      for (const key of ["title", "summary", "how", "small_objects", "none", "col_class", "col_minimum", "col_within", "col_beyond", "col_cost",
        "days_one", "days_other", "amount_one", "amount_other", "remaining_cost", "no_remaining", "releases_title", "release",
        "estimated_versions_one", "estimated_versions_other", "disclaimer", "list_price"]) {
        expect(messages[`minimum_storage.${key}`], `${locale} ${key}`).toBeTruthy();
      }
    }
  });
});
