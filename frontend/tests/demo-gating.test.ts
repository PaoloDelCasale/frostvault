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
    // The per-Vault recovery screenshot entry was retired with crypt (#419).
    expect(app).not.toContain("VaultCreateScreenshotFixture");
    expect(app).not.toContain("getDemoSearchParam");
    expect(browser).toMatch(/getDemoSearchParam\("(?:sheet|history|confirm|target|versions)"\)/);
    expect(operations).toMatch(
      /DEMO_MODE_ENABLED\s*\?\s*demoConfirm\s*:\s*null/,
    );
    expect(operations).not.toContain("demoVersionsPath");
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

  it("previews operation-policy paths inside the demo without calling a backend", async () => {
    vi.resetModules();
    vi.doMock("@/demoGate", () => ({
      DEMO_MODE_ENABLED: true,
      getDemoSearchParam: () => null,
    }));
    const originalFetch = window.fetch;

    try {
      const { installDemoFilesFetch } = await import(
        "@/pages/archive/demoFiles"
      );
      installDemoFilesFetch();

      const saveResponse = await window.fetch("/api/vault/operation-policy", {
        method: "PUT",
        body: JSON.stringify({ include_globs: ["test/"], exclude_globs: [] }),
      });
      expect(saveResponse.ok).toBe(true);

      const previewResponse = await window.fetch(
        "/api/vault/operation-policy/preview-globs",
        {
          method: "POST",
          body: JSON.stringify({
            paths: ["photos/images.jpg"],
            include_globs: ["test/"],
            exclude_globs: [],
          }),
        },
      );
      expect(previewResponse.ok).toBe(true);
      await expect(previewResponse.json()).resolves.toEqual({
        included: [],
        excluded: ["photos/images.jpg"],
      });
    } finally {
      window.fetch = originalFetch;
      vi.doUnmock("@/demoGate");
      vi.resetModules();
    }
  });

  it("persists the single Vault storage plan inside the demo response", async () => {
    vi.resetModules();
    vi.doMock("@/demoGate", () => ({
      DEMO_MODE_ENABLED: true,
      getDemoSearchParam: () => null,
    }));
    const originalFetch = window.fetch;

    try {
      const { installDemoFilesFetch } = await import(
        "@/pages/archive/demoFiles"
      );
      installDemoFilesFetch();

      const response = await window.fetch(
        "/api/vault/lifecycle",
        {
          method: "PUT",
          body: JSON.stringify({
            guided_profile: "archive_tiered",
          }),
        },
      );
      expect(response.ok).toBe(true);
      const lifecycle = (await response.json()) as {
        profile: { transitions: Array<{ days: number; storage_class: string }> };
        guided_profiles: Record<string, unknown>;
      };
      expect(lifecycle.guided_profiles.archive_tiered).toBeTruthy();
      expect(lifecycle.profile.transitions).toHaveLength(3);

      const reloaded = await window.fetch("/api/vault/lifecycle");
      await expect(reloaded.json()).resolves.toMatchObject({
        profile: lifecycle.profile,
      });
    } finally {
      window.fetch = originalFetch;
      vi.doUnmock("@/demoGate");
      vi.resetModules();
    }
  });
});
