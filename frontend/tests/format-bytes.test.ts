import { describe, expect, it } from "vitest";

import { formatBytes, formatCount } from "@/pages/archive/format";

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

describe("formatCount", () => {
  it("formats thousands with grouping separators", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCount(12)).toBe("12");
    expect(formatCount(1234)).toBe("1,234");
  });
});
