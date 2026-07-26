import { describe, expect, it } from "vitest";

import {
  formatStorageClassOptionLabel,
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
        return `${params?.id} — €${params?.rate}/GiB·mo · Instant retrieval`;
      }
      return key;
    });
    expect(label).toContain("STANDARD");
    expect(label).toContain("0.023");
    expect(label).toContain("Instant");
  });

  it("includes restore latency and restore rate for DEEP_ARCHIVE", () => {
    const label = formatStorageClassOptionLabel(deepArchive, (key, params) => {
      if (key === "ui.storage_class_option_restore") {
        return `${params?.id} — €${params?.rate}/GiB·mo · Restore ~${params?.hours}h · €${params?.restore_rate}/GiB restore · Min ${params?.min_days}d`;
      }
      return key;
    });
    expect(label).toContain("DEEP_ARCHIVE");
    expect(label).toContain("0.00099");
    expect(label).toContain("48");
    expect(label).toContain("0.0025");
    expect(label).toContain("180");
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
