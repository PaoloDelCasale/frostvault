import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const indexPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../index.html",
);

const cssPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../src/index.css",
);

describe("dark theme first paint", () => {
  it("runs the safe storage/system resolver before the module app script", () => {
    const html = readFileSync(indexPath, "utf8");
    const resolver = html.indexOf("frostvault_theme_active_user");
    const app = html.indexOf('src="/src/main.tsx"');

    expect(resolver).toBeGreaterThanOrEqual(0);
    expect(html).toContain("window.localStorage");
    expect(html).toContain("window.matchMedia");
    expect(html).toContain("document.documentElement.dataset.theme");
    expect(html).toContain("document.documentElement.style.colorScheme");
    expect(app).toBeGreaterThan(resolver);
  });

  it("declares explicit semantic dark tokens and native color-scheme", () => {
    const css = readFileSync(cssPath, "utf8");
    expect(css).toMatch(/:root\s*\{[\s\S]*color-scheme:\s*light/);
    expect(css).toContain('[data-theme="dark"]');
    expect(css).toContain("color-scheme: dark");
    expect(css).toContain("--state-both-bg");
    expect(css).toContain("--state-mixed-bg");
    expect(css).toContain("--storage-glacier-border");
    expect(css).toContain("--toast-success-bg");
    expect(css).toContain("--overlay");
  });
});
