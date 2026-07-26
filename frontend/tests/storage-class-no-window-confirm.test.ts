import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

describe("storage-class / lifecycle-pin flows — no window.confirm (seam 12)", () => {
  it("archive action modules do not call window.confirm or window.prompt", () => {
    const files = [
      "src/pages/archive/actions.ts",
      "src/pages/archive/FileOperationsHost.tsx",
      "src/pages/archive/StorageClassDialog.tsx",
      "src/pages/vault-access/LifecyclePanel.tsx",
    ];
    for (const relative of files) {
      const source = readFileSync(path.join(root, relative), "utf8");
      expect(source, relative).not.toMatch(/window\.confirm/);
      expect(source, relative).not.toMatch(/window\.prompt/);
    }
  });
});
