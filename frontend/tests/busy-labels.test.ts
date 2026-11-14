import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../src");
const locales = path.resolve(here, "../../app/locales");

function listSourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) return listSourceFiles(full);
    return /\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry) ? [full] : [];
  });
}

// #655: in Italian «Caricamento» is the upload, so a button waiting on its
// action names that action instead of the generic page-loading text.
describe("busy labels of action buttons", () => {
  it("uses the generic loading text only as status text, never as a button label", () => {
    const offenders: string[] = [];
    for (const file of listSourceFiles(root)) {
      readFileSync(file, "utf8").split("\n").forEach((line, index) => {
        if (!line.includes('t("ui.loading")')) return;
        // Allowed only as the whole text of a status paragraph.
        if (!/<p role=[^>]*>\{[^{}]*t\("ui\.loading"\)\}<\/p>/.test(line)) offenders.push(`${path.relative(root, file)}:${index + 1}`);
      });
    }
    expect(offenders).toEqual([]);
  });

  it("does not say «Caricamento» for anything but an upload in Italian waiting texts", () => {
    const it = JSON.parse(readFileSync(path.join(locales, "it.json"), "utf8")) as Record<string, string>;
    const waiting = Object.entries(it).filter(([key, value]) => /(^|[._])loading/.test(key) || value.endsWith("…"));
    expect(waiting.filter(([, value]) => /caricament/i.test(value)).map(([key]) => key)).toEqual([]);
    expect(it["ui.recover_starting"]).toBe("Avvio…");
    expect(it["ui.free_space_running"]).toBe("Liberazione in corso…");
  });
});
