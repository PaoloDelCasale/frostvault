import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const cssPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../src/index.css",
);

/** FrostVault palette hex literals — independent source of truth for the migration. */
const EXPECTED_TOKENS: Record<string, string> = {
  "--ink": "#18221d",
  "--muted": "#65716a",
  "--line": "#dce4de",
  "--surface": "#ffffff",
  "--canvas": "#f4f7f4",
  "--green": "#257a4b",
  "--green-soft": "#e2f3e9",
  "--blue-soft": "#e5effb",
  "--amber-soft": "#fff1cc",
  "--red-soft": "#fde8e5",
  "--violet-soft": "#eee9fb",
};

/** Radii from the FrostVault shape tokens — independent literals. */
const EXPECTED_RADII: Record<string, string> = {
  "--radius-card": "14px",
  "--radius-panel": "18px",
  "--radius-auth": "22px",
  "--radius-badge": "999px",
};

describe("FrostVault palette in @theme", () => {
  it("keeps every migrated token hex value", () => {
    const css = readFileSync(cssPath, "utf8");
    expect(css).toMatch(/@theme\b/);
    for (const [token, hex] of Object.entries(EXPECTED_TOKENS)) {
      const pattern = new RegExp(`${token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*:\\s*${hex}`, "i");
      expect(css, `${token} must be ${hex}`).toMatch(pattern);
    }
  });

  it("keeps card/panel/auth/badge radii", () => {
    const css = readFileSync(cssPath, "utf8");
    for (const [token, value] of Object.entries(EXPECTED_RADII)) {
      const pattern = new RegExp(
        `${token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*:\\s*${value}`,
        "i",
      );
      expect(css, `${token} must be ${value}`).toMatch(pattern);
    }
  });
});
