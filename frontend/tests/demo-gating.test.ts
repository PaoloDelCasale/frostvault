import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

const srcRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../src",
);

async function source(relativePath: string): Promise<string> {
  return readFile(path.join(srcRoot, relativePath), "utf8");
}

describe("capture/demo seams", () => {
  afterEach(() => {
    window.history.replaceState({}, "", "/");
  });

  it("centralizes the production gate and keeps every SPA entry seam behind it", async () => {
    const [gate, main, app, browser, operations] = await Promise.all([
      source("demoGate.ts"),
      source("main.tsx"),
      source("App.tsx"),
      source("pages/archive/FileBrowser.tsx"),
      source("pages/archive/FileOperationsHost.tsx"),
    ]);

    expect(gate).toMatch(
      /import\.meta\.env\.DEV\s*\|\|\s*import\.meta\.env\.VITE_ALLOW_DEMO\s*===\s*["']1["']/,
    );
    expect(main).toMatch(
      /if\s*\(\s*DEMO_MODE_ENABLED\s*&&[\s\S]*installDemoFilesFetch\(\)/,
    );
    expect(app).toMatch(
      /DEMO_MODE_ENABLED\s*&&[\s\S]*getDemoSearchParam\("demo"\)/,
    );
    expect(browser).toMatch(/getDemoSearchParam\("(?:sheet|history|confirm|target|versions|offline)"\)/);
    expect(browser).toMatch(/DEMO_MODE_ENABLED\s*&&\s*getDemoSearchParam\("offline"\)/);
    expect(operations).toMatch(
      /DEMO_MODE_ENABLED\s*\?\s*demoConfirm\s*:\s*null/,
    );
    expect(operations).toMatch(
      /DEMO_MODE_ENABLED\s*\?\s*demoVersionsPath\s*:\s*null/,
    );
  });

  it("does not patch fetch when the capture gate is off", async () => {
    vi.resetModules();
    vi.doMock("@/demoGate", () => ({
      DEMO_MODE_ENABLED: false,
      getDemoSearchParam: () => null,
    }));

    try {
      const { installDemoFilesFetch } = await import(
        "@/pages/archive/demoFiles"
      );
      const originalFetch = window.fetch;

      installDemoFilesFetch();

      expect(window.fetch).toBe(originalFetch);
    } finally {
      vi.doUnmock("@/demoGate");
      vi.resetModules();
    }
  });
});
