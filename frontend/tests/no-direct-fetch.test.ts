import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const srcRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../src",
);
const apiRoot = path.join(srcRoot, "api");

async function collectSourceFiles(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { withFileTypes: true });
  const files: string[] = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await collectSourceFiles(full)));
      continue;
    }
    if (
      /\.(ts|tsx)$/.test(entry.name) &&
      !entry.name.endsWith(".test.ts") &&
      !entry.name.endsWith(".test.tsx")
    ) {
      files.push(full);
    }
  }
  return files;
}

describe("fetch confinement", () => {
  it("does not call fetch directly outside frontend/src/api", async () => {
    const files = await collectSourceFiles(srcRoot);
    const offenders: string[] = [];
    const fetchCall = /(?<![\w$.])fetch\s*\(/;

    for (const file of files) {
      if (file === apiRoot || file.startsWith(apiRoot + path.sep)) {
        continue;
      }
      const source = await readFile(file, "utf8");
      if (fetchCall.test(source)) {
        offenders.push(path.relative(srcRoot, file));
      }
    }

    expect(offenders).toEqual([]);
  });
});
