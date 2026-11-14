import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const srcRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../src");

async function sourceFiles(dir: string): Promise<string[]> {
  const out: string[] = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await sourceFiles(full)));
    else if (/\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

// String(error) shows «ApiError: …» / «TypeError: Failed to fetch»; use errorMessage(e, t) (#651).
const CAUGHT = String.raw`(?:error|err|e|reason|cause|failure|exception|[\w$]+\.error)`;
const RAW = new RegExp(String.raw`String\(\s*${CAUGHT}\s*\)|\$\{\s*(?:error|err|cause|failure|exception|[\w$]+\.error)\s*\}`);

describe("readable errors", () => {
  it("never renders String(error) or `${error}` in frontend/src", async () => {
    const offenders: string[] = [];
    for (const file of await sourceFiles(srcRoot)) {
      (await readFile(file, "utf8")).split("\n").forEach((line, index) => {
        if (RAW.test(line)) offenders.push(`${path.relative(srcRoot, file)}:${index + 1}`);
      });
    }
    expect(offenders).toEqual([]);
  });
});
