import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

describe("PWA toolchain (seams 1–2)", () => {
  it("configures vite-plugin-pwa with FrostVault manifest and injectManifest SW", () => {
    const viteConfig = readFileSync(path.join(root, "vite.config.ts"), "utf8");
    expect(viteConfig).toMatch(/VitePWA/);
    expect(viteConfig).toMatch(/strategies:\s*"injectManifest"/);
    expect(viteConfig).toMatch(/name:\s*"FrostVault"/);
    expect(viteConfig).toMatch(/display:\s*"standalone"/);
    expect(viteConfig).toMatch(/pwa-192\.png/);
    expect(viteConfig).toMatch(/pwa-512\.png/);
  });

  it("registers the service worker at app bootstrap", () => {
    const main = readFileSync(path.join(root, "src/main.tsx"), "utf8");
    expect(main).toMatch(/registerFrostVaultServiceWorker/);
    const sw = readFileSync(path.join(root, "src/sw.ts"), "utf8");
    expect(sw).toMatch(/precacheAndRoute/);
    expect(sw).toMatch(/offlineFileServiceWorkerCacheName/);
    expect(sw).not.toMatch(/cacheName:\s*"frostvault-file-listing"/);
    expect(sw).toMatch(/addEventListener\(\s*"push"/);
  });
});
