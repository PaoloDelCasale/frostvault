import { describe, expect, it } from "vitest";

import {
  buildQuotaUpdatePayload,
  emptyQuotaFormValues,
  limitsToFormValues,
  quotaStatusItems,
} from "@/pages/admin/quota";

describe("buildQuotaUpdatePayload", () => {
  it("maps blank limits to null (unlimited)", () => {
    const form = emptyQuotaFormValues();
    form.reason = "remove quota limits";
    const result = buildQuotaUpdatePayload(form);
    expect(result).toEqual({
      ok: true,
      payload: {
        storage_soft_limit_bytes: null,
        storage_hard_limit_bytes: null,
        concurrency_soft_limit: null,
        concurrency_hard_limit: null,
        restore_30d_soft_limit_bytes: null,
        restore_30d_hard_limit_bytes: null,
        reason: "remove quota limits",
      },
    });
  });

  it("rejects soft > hard without building a payload", () => {
    const form = {
      ...emptyQuotaFormValues(),
      storage_soft_limit_bytes: "9",
      storage_hard_limit_bytes: "4",
      reason: "bad order",
    };
    const result = buildQuotaUpdatePayload(form);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toMatch(/cannot exceed/i);
    }
  });

  it("rejects an empty reason", () => {
    const form = {
      ...emptyQuotaFormValues(),
      storage_soft_limit_bytes: "10",
      storage_hard_limit_bytes: "20",
      reason: "   ",
    };
    const result = buildQuotaUpdatePayload(form);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toMatch(/reason/i);
    }
  });
});

describe("limitsToFormValues", () => {
  it("renders null limits as blank inputs", () => {
    expect(
      limitsToFormValues({
        storage_soft_limit_bytes: null,
        storage_hard_limit_bytes: 20,
      }).storage_soft_limit_bytes,
    ).toBe("");
    expect(
      limitsToFormValues({
        storage_soft_limit_bytes: null,
        storage_hard_limit_bytes: 20,
      }).storage_hard_limit_bytes,
    ).toBe("20");
  });
});

describe("quotaStatusItems", () => {
  it("formats warning decisions and unavailable evaluation", () => {
    expect(
      quotaStatusItems({
        state: "evaluated",
        allowed: true,
        decisions: [
          { code: "quota.storage.soft_exceeded", severity: "warning" },
        ],
      })[0]?.label,
    ).toBe("Warning: quota.storage.soft_exceeded");

    expect(quotaStatusItems(undefined)[0]?.label).toBe(
      "Quota state unavailable.",
    );
  });
});
