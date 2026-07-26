import { describe, expect, it } from "vitest";

import {
  formatStorageClassOptionLabel,
  formatStorageClassRecovery,
  formatStorageClassRetrieval,
  sourceNeedsRestoreForClassChange,
  type StorageClassOption,
} from "@/pages/archive/storageClassOptions";

const deepArchive: StorageClassOption = {
  id: "DEEP_ARCHIVE",
  currency: "EUR",
  storage_rate_eur_per_gib_month: 0.00099,
  retrieval: "restore",
  min_duration_days: 180,
  requires_restore: true,
  availability_zones: "multi",
  restore_hours_bulk: 48,
  restore_rate_eur_per_gib_bulk: 0.0025,
};

const standard: StorageClassOption = {
  id: "STANDARD",
  currency: "EUR",
  storage_rate_eur_per_gib_month: 0.023,
  retrieval: "instant",
  min_duration_days: 0,
  requires_restore: false,
  availability_zones: "multi",
};

describe("storage class option labels (seam 6)", () => {
  it("includes EUR per GiB-month and instant retrieval for STANDARD", () => {
    const label = formatStorageClassOptionLabel(standard, (key, params) => {
      if (key === "ui.storage_class_option_instant") {
        return `${params?.id} — €${params?.rate}/GiB·mo · Instant retrieval · Recovery —`;
      }
      if (key === "ui.storage_class_rate") return `€${params?.rate}/GiB·mo`;
      if (key === "ui.storage_class_retrieval_instant") return "Instant retrieval";
      if (key === "ui.storage_class_recovery_none") return "Recovery —";
      return key;
    });
    expect(label).toContain("STANDARD");
    expect(label).toContain("0.023");
    expect(label).toContain("Instant");
    expect(label).toContain("Recovery —");
    expect(label).not.toMatch(/Min\s+\d/);
  });

  it("includes restore latency and recovery price for DEEP_ARCHIVE without min days", () => {
    const label = formatStorageClassOptionLabel(deepArchive, (key, params) => {
      if (key === "ui.storage_class_option_restore") {
        return `${params?.id} — €${params?.rate}/GiB·mo · Restore ~${params?.hours}h · Recovery €${params?.restore_rate}/GiB`;
      }
      if (key === "ui.storage_class_rate") return `€${params?.rate}/GiB·mo`;
      if (key === "ui.storage_class_retrieval_restore") {
        return `Restore ~${params?.hours}h`;
      }
      if (key === "ui.storage_class_recovery_price") {
        return `Recovery €${params?.restore_rate}/GiB`;
      }
      return key;
    });
    expect(label).toContain("DEEP_ARCHIVE");
    expect(label).toContain("0.00099");
    expect(label).toContain("48");
    expect(label).toContain("0.0025");
    expect(label).toContain("Recovery");
    expect(label).not.toMatch(/Min\s+\d/);
    expect(label).not.toContain("180");
  });

  it("includes instant retrieval fee for GLACIER_IR", () => {
    const glacierIr: StorageClassOption = {
      id: "GLACIER_IR",
      currency: "EUR",
      storage_rate_eur_per_gib_month: 0.004,
      retrieval: "instant",
      min_duration_days: 90,
      requires_restore: false,
      availability_zones: "multi",
      retrieval_rate_eur_per_gib: 0.03,
    };
    expect(
      formatStorageClassRecovery(glacierIr, (key, params) =>
        key === "ui.storage_class_recovery_price"
          ? `Recovery €${params?.restore_rate}/GiB`
          : key,
      ),
    ).toBe("Recovery €0.03/GiB");
    expect(
      formatStorageClassRetrieval(glacierIr, (key) =>
        key === "ui.storage_class_retrieval_instant" ? "Instant retrieval" : key,
      ),
    ).toBe("Instant retrieval");
  });

  it("formats recovery price separately from retrieval", () => {
    expect(
      formatStorageClassRetrieval(standard, (key) =>
        key === "ui.storage_class_retrieval_instant" ? "Instant retrieval" : key,
      ),
    ).toBe("Instant retrieval");
    expect(
      formatStorageClassRecovery(standard, (key) =>
        key === "ui.storage_class_recovery_none" ? "Recovery —" : key,
      ),
    ).toBe("Recovery —");
    expect(
      formatStorageClassRecovery(deepArchive, (key, params) =>
        key === "ui.storage_class_recovery_price"
          ? `Recovery €${params?.restore_rate}/GiB`
          : key,
      ),
    ).toContain("0.0025");
  });

  it("detects restore need when warming from unrestored Deep Archive", () => {
    expect(
      sourceNeedsRestoreForClassChange({
        currentClass: "DEEP_ARCHIVE",
        restoreState: null,
      }),
    ).toBe(true);
    expect(
      sourceNeedsRestoreForClassChange({
        currentClass: "DEEP_ARCHIVE",
        restoreState: "available",
      }),
    ).toBe(false);
    expect(
      sourceNeedsRestoreForClassChange({
        currentClass: "STANDARD",
        restoreState: null,
      }),
    ).toBe(false);
  });
});
