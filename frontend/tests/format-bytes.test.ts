import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatCompactCount,
  formatCount,
  formatDate,
  formatEur,
  formatMoney,
  pickDurationUnit,
} from "@/pages/archive/format";
import { translate } from "@/i18n/translate";

describe("formatBytes", () => {
  // Independent expected values (not recomputed via /1024 loops).
  it.each([
    [0, "0 B"],
    [500, "500 B"],
    [1023, "1,023 B"],
    [1024, "1.0 KiB"],
    [1536, "1.5 KiB"],
    [1048576, "1.0 MiB"],
    [1073741824, "1.0 GiB"],
    [1099511627776, "1.0 TiB"],
  ] as const)("formats %s as %s", (value, expected) => {
    expect(formatBytes(value, "en")).toBe(expected);
  });

  it("uses binary units with the Italian decimal comma", () => {
    expect(formatBytes(1536, "it")).toBe("1,5 KiB");
    expect(formatBytes(1610612736, "it")).toBe("1,5 GiB");
    expect(formatBytes(1023, "it")).toBe("1023 B");
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

  it("formats other currencies, signed deltas and missing amounts", () => {
    expect(formatMoney(3.42, { currency: "USD", locale: "en" })).toBe("$3.42");
    expect(formatMoney(3.42, { locale: "it" })).toMatch(/^3,42\s€$/);
    expect(formatMoney(1.5, { locale: "en", signed: true })).toBe("+€1.50");
    expect(formatMoney(-1.5, { locale: "en", signed: true })).toBe("-€1.50");
    expect(formatMoney(0, { locale: "en", signed: true })).toBe("€0.00");
    expect(formatMoney(null)).toBe("—");
    expect(formatMoney("0.5", { locale: "en" })).toBe("€0.50");
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

describe("translate plurals", () => {
  const messages = {
    "files.selected_one": "{count} selezionato",
    "files.selected_other": "{count} selezionati",
    "only.other_other": "{count} items",
    "plain": "{count} things",
  };

  it("selects _one and _other with Intl.PluralRules", () => {
    expect(translate(messages, "files.selected", { count: 1 }, "it")).toBe("1 selezionato");
    expect(translate(messages, "files.selected", { count: 0 }, "it")).toBe("0 selezionati");
    expect(translate(messages, "files.selected", { count: 2 }, "en")).toBe("2 selezionati");
  });

  it("groups the count digits for the locale", () => {
    expect(translate(messages, "files.selected", { count: 1234 }, "en")).toBe("1,234 selezionati");
    expect(translate(messages, "files.selected", { count: 1234 }, "it")).toBe("1234 selezionati");
    expect(translate(messages, "files.selected", { count: 12345 }, "it")).toBe("12.345 selezionati");
  });

  it("falls back to _other, then to the bare key", () => {
    expect(translate(messages, "only.other", { count: 1 }, "en")).toBe("1 items");
    expect(translate(messages, "plain", { count: 1 }, "en")).toBe("1 things");
    expect(translate(messages, "files.selected", { count: "3" }, "en")).toBe("files.selected");
  });
});
