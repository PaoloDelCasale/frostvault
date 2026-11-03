import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { resetApiClientForTests } from "@/api";
import { validStoragePlan } from "@/pages/vault-access/storagePlan";
import { createVaultAccessFetch, defaultLifecycle, jsonResponse, loadCatalog, renderVaultAccess } from "./vault-access-harness";

afterEach(() => { cleanup(); resetApiClientForTests(); });

describe("Vault storage plan in Advanced", () => {
  it("offers four explained presets plus custom and saves one whole-Vault plan", async () => {
    const user = userEvent.setup();
    let payload: unknown;
    const fetchImpl = createVaultAccessFetch({
      "PUT /api/vault/lifecycle": (init) => {
        payload = JSON.parse(String(init?.body));
        return jsonResponse({ ...defaultLifecycle, profile: defaultLifecycle.guided_profiles.economical });
      },
    });
    renderVaultAccess({ fetchImpl });
    await user.click(await screen.findByText("Advanced", { exact: true }));
    await user.click(await screen.findByRole("combobox", { name: "Storage plan" }));
    expect(within(screen.getByRole("listbox")).getAllByRole("option")).toHaveLength(5);
    await user.click(screen.getByRole("option", { name: /Economical instant access/ }));
    expect(screen.getByText(/Standard-IA at day 30, Glacier Instant Retrieval at day 90:/, { selector: "p" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save plan" }));
    await waitFor(() => expect(payload).toEqual({ guided_profile: "economical" }));
    expect(screen.queryByLabelText(/folder path/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/noncurrent transitions/i)).not.toBeInTheDocument();
  });

  it("saves custom early Deep Archive and displays a pending sync honestly", async () => {
    const user = userEvent.setup();
    let payload: unknown;
    const fetchImpl = createVaultAccessFetch({
      "PUT /api/vault/lifecycle": (init) => {
        payload = JSON.parse(String(init?.body));
        return jsonResponse({ ...defaultLifecycle, profile: { transitions: [{ days: 1, storage_class: "DEEP_ARCHIVE" }] }, sync_pending: true });
      },
    });
    renderVaultAccess({ fetchImpl });
    await user.click(await screen.findByText("Advanced", { exact: true }));
    await user.click(await screen.findByRole("combobox", { name: "Storage plan" }));
    await user.click(screen.getByRole("option", { name: /^Custom/ }));
    await user.click(screen.getByRole("button", { name: "Add transition" }));
    await user.click(screen.getByRole("button", { name: "Save plan" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Use increasing nonnegative whole days");
    expect(payload).toBeUndefined();
    await user.type(screen.getByLabelText("After N days"), "1");
    await user.click(screen.getByRole("button", { name: "Save plan" }));
    await waitFor(() => expect(payload).toEqual({ profile: { transitions: [{ days: 1, storage_class: "DEEP_ARCHIVE" }] } }));
    expect(await screen.findByText(/Plan saved; cloud synchronization is pending/)).toBeInTheDocument();
  });

  it("validates object age, supported waterfall and source residence, not absolute archive minima", () => {
    const valid = (...steps: [number, string][]) => validStoragePlan(steps.map(([days, storage_class]) => ({ days: String(days), storage_class })));
    expect(valid([0, "DEEP_ARCHIVE"])).toBe(true);
    expect(valid([1, "DEEP_ARCHIVE"])).toBe(true);
    expect(valid([4, "GLACIER_IR"], [93, "DEEP_ARCHIVE"])).toBe(false);
    expect(valid([4, "GLACIER_IR"], [94, "DEEP_ARCHIVE"])).toBe(true);
    expect(valid([29, "STANDARD_IA"])).toBe(false);
    expect(valid([30, "STANDARD_IA"], [59, "GLACIER_IR"])).toBe(false);
    expect(valid([30, "STANDARD_IA"], [60, "GLACIER_IR"])).toBe(true);
    expect(valid([30, "STANDARD_IA"], [60, "ONEZONE_IA"])).toBe(true);
    expect(valid([30, "ONEZONE_IA"], [90, "GLACIER_IR"])).toBe(false);
    expect(valid([1, "DEEP_ARCHIVE"], [200, "GLACIER"])).toBe(false);
    expect(valid([1.5, "GLACIER"])).toBe(false);
  });

  it("has timing and cost explanations in both languages and no retired UI strings", () => {
    for (const locale of ["it", "en"] as const) {
      const messages = loadCatalog(locale);
      for (const name of ["standard_only", "economical", "archive_tiered", "deep_archive_early", "custom"]) {
        expect(messages[`access.plan_${name}`]).toBeTruthy();
        expect(messages[`access.plan_${name}_help`]).toBeTruthy();
      }
      expect(messages["access.plan_caveats"]).toContain("128 KB");
      expect(Object.keys(messages).some((key) => /lifecycle_pin|lifecycle_override|noncurrent_rules|storage_class_confirm/.test(key))).toBe(false);
    }
  });
});
