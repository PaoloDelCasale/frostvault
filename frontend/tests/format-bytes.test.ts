import { describe, expect, it } from "vitest";

import { formatBytes, formatCount, pickDurationUnit } from "@/pages/archive/format";

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
