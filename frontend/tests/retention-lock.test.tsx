import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";
import { problemLink } from "@/lib/problemLink";
import { vaultSettingsUrl } from "@/router";
import { createVaultAccessFetch, jsonResponse, renderVaultAccess } from "./vault-settings-harness";

afterEach(() => { cleanup(); resetApiClientForTests(); });

const vault = (retention_lock_days: number | null) => ({
  id: 1, uuid: "00000000-0000-4000-8000-000000000001", name: "Test Archive", source_root: "/sources/managed/x",
  s3_prefix: "", s3_bucket: "frostvault-test-ab12cd34", s3_region: "eu-south-1", dedicated_bucket: true,
  lifecycle: "active", paused: false, root_released_at: null, vault_deletion_status: null, retention_lock_days,
});

// #736: the Retention Lock is chosen at creation and shown read-only in the settings.
describe("Retention Lock settings section", () => {
  it("shows the mode, that it cannot change, and the versions it still holds back", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/general/vaults": () => jsonResponse({ items: [vault(180)] }),
      "GET /api/vaults/1/retention-lock": () => jsonResponse({ vault_id: 1, days: 180, mode: "GOVERNANCE",
        retained: { count: 4, bytes: 2 * 1024 ** 2, deletable_from: "2027-01-10T00:00:00+00:00", deletable_until: "2027-02-20T00:00:00+00:00",
          failed_count: 0, failed_attempts: 0, last_error: null, last_attempt_at: null, next_attempt_at: null } }),
    }, { locale: "it" }) });
    const section = (await screen.findByRole("heading", { name: "Retention Lock", level: 2 })).closest("section")!;
    expect(section).toHaveAttribute("id", "retention-lock");
    expect(await within(section).findByTestId("retention-lock-mode")).toHaveTextContent("Modalità Governance, 180 giorni.");
    expect(within(section).getByText("Scelto alla creazione del Vault: non si può cambiare.")).toBeInTheDocument();
    expect(within(section).getByTestId("retention-lock-retained"))
      .toHaveTextContent("4 versioni sostituite (2,0 MiB) sono ancora bloccate: eliminazione possibile dal 10 gen 2027 al 20 feb 2027.");
    expect(within(section).queryByRole("checkbox")).toBeNull();
    expect(within(section).queryByTestId("retention-lock-failed")).toBeNull();
  });

  // #749: a sweep that keeps failing is visible, with its error and next attempt.
  it("says which versions could not be deleted, why and when it tries again", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/general/vaults": () => jsonResponse({ items: [vault(30)] }),
      "GET /api/vaults/1/retention-lock": () => jsonResponse({ vault_id: 1, days: 30, mode: "GOVERNANCE",
        retained: { count: 2, bytes: 10, deletable_from: "2027-01-10T00:00:00+00:00", deletable_until: "2027-01-12T00:00:00+00:00",
          failed_count: 2, failed_attempts: 3, last_error: "AccessDenied", last_attempt_at: "2027-01-13T09:00:00+00:00",
          next_attempt_at: "2027-01-14T09:00:00+00:00" } }),
    }, { locale: "it" }) });
    const failed = await screen.findByTestId("retention-lock-failed");
    expect(failed).toHaveTextContent("2 versioni non sono ancora state eliminate (ultimo errore: AccessDenied, fino a 3 tentativi di fila).");
    expect(failed).toHaveTextContent("Prossimo tentativo: 14 gen 2027");
  });

  it("has no section for a Vault without one", async () => {
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/general/vaults": () => jsonResponse({ items: [vault(null)] }),
    }) });
    await screen.findByRole("heading", { name: "Cloud duplicates", level: 2 });
    expect(screen.queryByRole("heading", { name: "Retention Lock", level: 2 })).toBeNull();
  });

  it("links a Delete Marker Problem to the section", () => {
    expect(problemLink(1, { retention_lock: true, vault: "Photos", count: 1 })).toBe(vaultSettingsUrl(1, "retention-lock"));
  });
});
