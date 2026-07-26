import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../src");

function listSourceFiles(dir: string): string[] {
  const entries = readdirSync(dir);
  const files: string[] = [];
  for (const entry of entries) {
    const full = path.join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      files.push(...listSourceFiles(full));
    } else if (/\.(css|tsx|ts)$/.test(entry) && !entry.endsWith(".test.ts") && !entry.endsWith(".test.tsx")) {
      files.push(full);
    }
  }
  return files;
}

/**
 * Acceptance: no CSS rule may rely solely on :hover to convey state.
 * Allow brightness/filter tweaks on already-visible controls, but ban
 * colour/visibility changes that appear only under :hover.
 */
describe("No hover-only state affordances", () => {
  it("does not introduce :hover rules that alone change colour or visibility", () => {
    const offenders: string[] = [];
    for (const file of listSourceFiles(root)) {
      const source = readFileSync(file, "utf8");
      const hoverOnlyColour = /hover:(?:text|bg|border|opacity|invisible|hidden)-/g;
      // brightness/filter on buttons is ok (control already visible); colour swaps are not
      let match: RegExpExecArray | null;
      while ((match = hoverOnlyColour.exec(source)) !== null) {
        const snippet = source.slice(Math.max(0, match.index - 40), match.index + 80);
        // Directory/link colour that appears only on hover is forbidden
        if (/hover:text-green|hover:bg-\[|hover:opacity-0|hover:invisible|hover:hidden/.test(snippet)) {
          offenders.push(`${path.relative(root, file)}: ${match[0]}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
