import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { BADGE_STATE_VARIANT_CLASSES } from "@/components/Badge";
import { buttonVariants } from "@/components/ui/button";

import { MINIMUM_CONTRAST, contrastRatio, paletteTokens, resolvedHex } from "./theme-tokens";

// Main theme pairs (#653): text on surfaces, buttons and the most frequent states.
const PAIRS: ReadonlyArray<{ name: string; foreground: string; background: string }> = [
  { name: "ink on surface", foreground: "--ink", background: "--surface" },
  { name: "ink on canvas", foreground: "--ink", background: "--canvas" },
  { name: "muted on surface", foreground: "--muted", background: "--surface" },
  { name: "muted on canvas", foreground: "--muted", background: "--canvas" },
  { name: "primary button", foreground: "--primary-foreground", background: "--primary" },
  { name: "danger button", foreground: "--destructive-foreground", background: "--destructive" },
  { name: "destructive text on surface", foreground: "--destructive", background: "--surface" },
  { name: "destructive text on canvas", foreground: "--destructive", background: "--canvas" },
  ...(["problem", "pending", "protected"] as const).map((state) => {
    const classes = BADGE_STATE_VARIANT_CLASSES[state];
    const token = (utility: "bg" | "text") => {
      const match = classes.match(new RegExp(`${utility}-\\[var\\((--[\\w-]+)\\)\\]`));
      if (!match) throw new Error(`${state}: missing ${utility} token in ${classes}`);
      return match[1];
    };
    return { name: `${state} badge`, foreground: token("text"), background: token("bg") };
  }),
];

const CASES = (["light", "dark"] as const).flatMap((palette) =>
  PAIRS.map((pair) => ({ palette, ...pair })),
);

describe("theme token contrast", () => {
  it.each(CASES)("$palette $name meets WCAG AA", ({ palette, foreground, background }) => {
    const tokens = paletteTokens(palette);
    const foregroundHex = resolvedHex(tokens, foreground);
    const backgroundHex = resolvedHex(tokens, background);
    const ratio = contrastRatio(foregroundHex, backgroundHex);

    expect(
      ratio,
      `${palette}: ${foreground} ${foregroundHex} on ${background} ${backgroundHex} has contrast ${ratio.toFixed(3)}:1`,
    ).toBeGreaterThanOrEqual(MINIMUM_CONTRAST);
  });

  it("danger buttons use the destructive theme tokens", () => {
    for (const variant of ["danger", "destructive"] as const) {
      const classes = buttonVariants({ variant }).split(" ");
      expect(classes).toContain("bg-destructive");
      expect(classes).toContain("text-destructive-foreground");
      expect(classes).not.toContain("text-white");
    }
  });
});

const srcRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../src");

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(full);
    return /\.(tsx?|css)$/.test(entry.name) ? [full] : [];
  });
}

describe("no fixed Tailwind red classes", () => {
  it("components use theme tokens instead of red/rose palette utilities", () => {
    const fixedRed = /\b[a-z-]+-(?:red|rose)-\d{2,3}\b/g;
    const offenders = sourceFiles(srcRoot).flatMap((file) =>
      [...readFileSync(file, "utf8").matchAll(fixedRed)].map(
        (match) => `${path.relative(srcRoot, file)}: ${match[0]}`,
      ),
    );
    expect(offenders).toEqual([]);
  });
});
