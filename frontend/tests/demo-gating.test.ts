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

  it("persists lifecycle overrides inside the demo response", async () => {
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
        "/api/vault/lifecycle/folder-overrides",
        {
          method: "PUT",
          body: JSON.stringify({
            folder_path: "test",
            guided_profile: "archive_tiered",
          }),
        },
      );
      expect(response.ok).toBe(true);
      const lifecycle = (await response.json()) as {
        folder_overrides: Array<{ folder_path: string; policy_id: string }>;
        guided_profiles: Record<string, unknown>;
      };
      expect(lifecycle.guided_profiles.archive_tiered).toBeTruthy();
      expect(lifecycle.folder_overrides).toEqual([
        { folder_path: "test", policy_id: "demo-folder:test" },
      ]);

      const reloaded = await window.fetch("/api/vault/lifecycle");
      await expect(reloaded.json()).resolves.toMatchObject({
        folder_overrides: [
          { folder_path: "test", policy_id: "demo-folder:test" },
        ],
      });
    } finally {
      window.fetch = originalFetch;
      vi.doUnmock("@/demoGate");
      vi.resetModules();
    }
  });

  it("provides realistic Vault audit events in the demo", async () => {
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

      const response = await window.fetch("/api/audit-events");
      const payload = (await response.json()) as {
        events: Array<{
          event: string;
          actor_user_id: number | null;
          detail: Record<string, unknown>;
        }>;
      };
      expect(payload.events).toHaveLength(7);
      expect(payload.events.map((event) => event.event)).toEqual(
        expect.arrayContaining([
          "vault_file_renamed",
          "vault_lifecycle_default_updated",
          "operation_policy_updated",
          "catalog_scan_completed",
        ]),
      );
      expect(payload.events.some((event) => event.actor_user_id === null)).toBe(true);
      expect(payload.events.some((event) => "path" in event.detail)).toBe(true);
    } finally {
      window.fetch = originalFetch;
      vi.doUnmock("@/demoGate");
      vi.resetModules();
    }
  });

  it("keeps archive.pdf versions consistent between Path History and Recover", async () => {
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

      const [historyResponse, versionsResponse] = await Promise.all([
        window.fetch("/api/file-history?path=archive.pdf"),
        window.fetch("/api/files/versions?path=archive.pdf"),
      ]);
      const history = (await historyResponse.json()) as {
        versions: Array<{
          version_number?: number;
          uploaded_at?: string | null;
          storage_class?: string | null;
          size?: number | null;
          object_key?: string;
        }>;
      };
      const recover = (await versionsResponse.json()) as {
        items: Array<{
          version_number: number;
          created_at?: string | null;
          storage_class?: string | null;
          size?: number | null;
          object_key?: string;
        }>;
        recoverable_count: number;
      };

      expect(history.versions).toHaveLength(recover.items.length);
      expect(
        history.versions.map((version) => ({
          number: version.version_number,
          date: version.uploaded_at,
          storage: version.storage_class,
          size: version.size,
        })),
      ).toEqual(
        recover.items.map((version) => ({
          number: version.version_number,
          date: version.created_at,
          storage: version.storage_class,
          size: version.size,
        })),
      );
      expect(recover.recoverable_count).toBe(recover.items.length);
      expect(
        recover.items.map((version) => version.storage_class),
      ).toEqual(["DEEP_ARCHIVE", "DEEP_ARCHIVE"]);
    } finally {
      window.fetch = originalFetch;
      vi.doUnmock("@/demoGate");
      vi.resetModules();
    }
  });
});
