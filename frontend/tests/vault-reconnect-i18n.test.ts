import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const components = ["ReconnectVaultDialog", "RecoveryKitSection"].map((name) => readFileSync(path.join(root, `frontend/src/pages/general/${name}.tsx`), "utf8")).join("\n");
const en: Record<string, string> = JSON.parse(readFileSync(path.join(root, "app/locales/en.json"), "utf8"));
const itMessages: Record<string, string> = JSON.parse(readFileSync(path.join(root, "app/locales/it.json"), "utf8"));

describe("Reconnect and installation kit English/Italian contract", () => {
  it("localizes prompts without retaining superseded relocation options", () => {
    const keys = [...components.matchAll(/t\("(general\.reconnect[^"]*|recovery_kit\.[^"]*)"/g)].map((match) => match[1]!);
    expect(keys.length).toBeGreaterThan(15);
    for (const key of new Set(keys)) {
      expect(en[key], key).toBeTruthy();
      expect(itMessages[key], key).toBeTruthy();
      expect(itMessages[key]).not.toBe(en[key]);
    }
    expect(Object.keys(en).some((key) => key.startsWith("general.relocation") || key === "general.relocate_vault")).toBe(false);
  });
  it("explains full scan, conflicts, non-catalog custody and restore warning", () => {
    expect(en["general.reconnect_scan_warning"]).toMatch(/full scan.*conflicts/i);
    expect(itMessages["general.reconnect_scan_warning"]).toMatch(/scansione completa.*conflitti/i);
    expect(en["recovery_kit.help"]).toMatch(/no catalog/i);
    expect(en["recovery_kit.restore_warning"]).toContain("{date}");
    expect(en["recovery_kit.restore_warning"]).toContain("{count}");
  });
});
