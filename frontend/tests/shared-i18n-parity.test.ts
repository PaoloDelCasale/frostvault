import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const srcRoot = path.join(frontendRoot, "src");
const localesDir = path.resolve(frontendRoot, "../app/locales");

/** Temporary toolchain screen from #57; page issues own real copy. */
const EXCLUDED_FILES = new Set(["PlaceholderScreen.tsx"]);

async function collectTsxFiles(dir: string): Promise<string[]> {
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
  const files: string[] = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "ui") continue;
      files.push(...(await collectTsxFiles(full)));
      continue;
    }
    if (
      entry.name.endsWith(".tsx") &&
      !entry.name.endsWith(".test.tsx") &&
      !EXCLUDED_FILES.has(entry.name)
    ) {
      files.push(full);
    }
  }
  return files;
}

async function sharedComponentFiles(): Promise<string[]> {
  return [
    ...(await collectTsxFiles(path.join(srcRoot, "components"))),
    ...(await collectTsxFiles(path.join(srcRoot, "i18n"))),
    ...(await collectTsxFiles(path.join(srcRoot, "pages"))),
  ];
}

/** Same-line JSX text: <tag>Visible copy</tag> (not an expression). */
const SAME_LINE_JSX_TEXT =
  /<[A-Za-z][A-Za-z0-9]*(?:\s[^>]*)?>\s*([^<{]+?)\s*<\/[A-Za-z]/g;

/**
 * Multiline JSX text child sitting alone between tags.
 *
 * The capture must start with a non-whitespace, non-`{` character so that
 * `\s+` cannot backtrack and leave a leading space that would admit
 * `{children}` / `{label}` as a false positive.
 */
const MULTILINE_JSX_TEXT =
  /<[A-Za-z][A-Za-z0-9]*(?:\s[^>]*)?>\s*\n[ \t]+([^<{}\s\n][^<\n]*[A-Za-zÀ-ÿ][^<\n]*)\n[ \t]*<\//g;

const T_KEY_CALL = /\bt\(\s*["']([^"']+)["']/g;

function findHardcodedStrings(source: string): string[] {
  const found: string[] = [];
  for (const pattern of [SAME_LINE_JSX_TEXT, MULTILINE_JSX_TEXT]) {
    for (const match of source.matchAll(pattern)) {
      const text = match[1].trim();
      // Expressions and interpolation must never count as copy.
      if (!text || text.includes("{") || text.includes("}")) {
        continue;
      }
      if (/[A-Za-zÀ-ÿ]{2,}/.test(text)) {
        found.push(text);
      }
    }
  }
  return found;
}

describe("shared component i18n coverage parity", () => {
  it("has no hardcoded visible strings in shared components", async () => {
    const files = await sharedComponentFiles();
    const offenders: string[] = [];

    for (const file of files) {
      const source = await readFile(file, "utf8");
      const hardcoded = findHardcodedStrings(source);
      if (hardcoded.length > 0) {
        offenders.push(
          `${path.relative(srcRoot, file)}: ${hardcoded.join("; ")}`,
        );
      }
    }

    expect(offenders).toEqual([]);
  });

  it("uses only keys present in both en.json and it.json catalogs", async () => {
    const [enRaw, itRaw] = await Promise.all([
      readFile(path.join(localesDir, "en.json"), "utf8"),
      readFile(path.join(localesDir, "it.json"), "utf8"),
    ]);
    const en = JSON.parse(enRaw) as Record<string, string>;
    const it = JSON.parse(itRaw) as Record<string, string>;

    const files = await sharedComponentFiles();
    const missing: string[] = [];

    for (const file of files) {
      const source = await readFile(file, "utf8");
      for (const match of source.matchAll(T_KEY_CALL)) {
        const key = match[1];
        if (!(key in en) || !(key in it)) {
          missing.push(`${path.relative(srcRoot, file)}: ${key}`);
        }
      }
    }

    expect(missing).toEqual([]);
  });
});
