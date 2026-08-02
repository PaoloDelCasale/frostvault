import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const component = readFileSync(
  path.join(root, "frontend/src/pages/admin/RelocateVaultDialog.tsx"),
  "utf8",
);
const en = JSON.parse(readFileSync(path.join(root, "app/locales/en.json"), "utf8")) as Record<string, string>;
const itMessages = JSON.parse(readFileSync(path.join(root, "app/locales/it.json"), "utf8")) as Record<string, string>;

describe("Vault relocation English/Italian UI contract", () => {
  it("localizes every relocation prompt in both supported locales", () => {
    const keys = [...component.matchAll(/t\("(admin\.relocation[^"]*|admin\.relocate_vault)"\)/g)].map(
      (match) => match[1]!,
    );
    expect(keys.length).toBeGreaterThanOrEqual(8);
    for (const key of new Set(keys)) {
      expect(en[key], `missing English ${key}`).toBeTruthy();
      expect(itMessages[key], `missing Italian ${key}`).toBeTruthy();
      expect(itMessages[key]).not.toBe(en[key]);
    }
  });

  it("warns that local work waits for the mandatory full scan", () => {
    expect(en["admin.relocation_scan_warning"]).toMatch(/full scan/i);
    expect(itMessages["admin.relocation_scan_warning"]).toMatch(/scansione completa/i);
  });
});
