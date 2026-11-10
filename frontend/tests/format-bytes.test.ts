import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatCompactCount,
  formatCount,
  formatDate,
  formatEur,
  pickDurationUnit,
} from "@/pages/archive/format";

describe("formatBytes", () => {
  // Independent expected values (not recomputed via /1024 loops).
  it.each([
    [0, "0 B"],
    [500, "500 B"],
    [1023, "1023 B"],
    [1024, "1.0 KB"],
    [1536, "1.5 KB"],
    [1048576, "1.0 MB"],
    [1073741824, "1.0 GB"],
    [1099511627776, "1.0 TB"],
  ] as const)("formats %s as %s", (value, expected) => {
    expect(formatBytes(value)).toBe(expected);
  });

  it("renders an em dash for nullish values", () => {
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(undefined)).toBe("—");
  });
});

describe("pickDurationUnit", () => {
  it.each([
    [0, { value: 0, unit: "seconds" }],
    [1, { value: 1, unit: "seconds" }],
    [59, { value: 59, unit: "seconds" }],
    [60, { value: 1, unit: "minutes" }],
    [90, { value: 1.5, unit: "minutes" }],
    [3600, { value: 1, unit: "hours" }],
    [5400, { value: 1.5, unit: "hours" }],
    [86400, { value: 24, unit: "hours" }],
  ] as const)("maps %s seconds to %j", (seconds, expected) => {
    expect(pickDurationUnit(seconds)).toEqual(expected);
  });
});

describe("formatCount", () => {
  it("formats thousands with grouping separators", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCount(12)).toBe("12");
    expect(formatCount(1234)).toBe("1,234");
  });
});

describe("formatCompactCount", () => {
  it("keeps small counts exact and compactifies large ones", () => {
    expect(formatCompactCount(0)).toBe("0");
    expect(formatCompactCount(999)).toBe("999");
    expect(formatCompactCount(1200)).toBe("1.2K");
    expect(formatCompactCount(1_000_000)).toBe("1M");
  });
});

describe("formatEur", () => {
  it.each([
    [0.270513, "en", "€0.27"],
    [0.03717, "en", "€0.04"],
    [0, "en", "€0.00"],
    [0.00002, "en", "< €0.01"],
    [0.009, "en", "< €0.01"],
    [0.01, "en", "€0.01"],
    [1234.5, "en", "€1,234.50"],
  ] as const)("formats %s in %s as %s", (value, locale, expected) => {
    expect(formatEur(value, locale)).toBe(expected);
  });

  it("uses the Italian decimal comma and trailing symbol", () => {
    expect(formatEur(0.270513, "it")).toMatch(/^0,27\s€$/);
    expect(formatEur(0.00002, "it")).toMatch(/^< 0,01\s€$/);
  });
});

describe("formatDate", () => {
  it("renders a localized calendar date instead of ISO", () => {
    expect(formatDate("2026-10-15T12:00:00Z", "en")).toBe("Oct 15, 2026");
    expect(formatDate("2026-10-15T12:00:00Z", "it")).toBe("15 ott 2026");
  });

  it("keeps date-only values on their calendar day and passes through junk", () => {
    expect(formatDate("2026-11-01", "en")).toBe("Nov 1, 2026");
    expect(formatDate("not a date", "en")).toBe("not a date");
    expect(formatDate(null, "en")).toBe("—");
  });
});
