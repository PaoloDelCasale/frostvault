import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { translate } from "./translate";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../app/locales",
);

function loadCatalog(locale: "en" | "it"): Record<string, string> {
  const raw = readFileSync(path.join(localesDir, `${locale}.json`), "utf8");
  return JSON.parse(raw) as Record<string, string>;
}

describe("translate (real catalogs)", () => {
  it("resolves an existing key from app/locales/en.json", () => {
    const messages = loadCatalog("en");
    expect(translate(messages, "ui.sign_out")).toBe("Sign out");
  });

  it("interpolates named parameters for ui.protected_archive", () => {
    const messages = loadCatalog("en");
    expect(translate(messages, "ui.protected_archive", { name: "Family" })).toBe(
      "Protected archive · Family",
    );
  });

  it("returns a visible fallback for a missing key, never an empty string", () => {
    const messages = loadCatalog("en");
    const result = translate(messages, "missing.not_defined");
    expect(result).toBe("missing.not_defined");
    expect(result.length).toBeGreaterThan(0);
  });
});
