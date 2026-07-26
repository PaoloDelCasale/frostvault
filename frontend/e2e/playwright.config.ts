import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig } from "@playwright/test";

const root = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(root, "../..");
const port = Number(process.env.E2E_PORT || "8080");
const baseURL = process.env.E2E_BASE_URL || `http://127.0.0.1:${port}`;

/** Prefer the repo venv locally; CI uses setup-python on PATH (no .venv). */
function resolvePython(): string {
  if (process.env.E2E_PYTHON) return process.env.E2E_PYTHON;
  const venvPython = path.join(repoRoot, ".venv", "bin", "python");
  if (existsSync(venvPython)) return venvPython;
  return "python3";
}

export default defineConfig({
  testDir: path.join(root, "tests"),
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never", outputFolder: "e2e-report" }]],
  outputDir: path.join(root, "test-results"),
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    {
      name: "mobile-375",
      use: {
        browserName: "chromium",
        viewport: { width: 375, height: 667 },
        isMobile: true,
        hasTouch: true,
      },
    },
    {
      name: "desktop-1280",
      use: {
        browserName: "chromium",
        viewport: { width: 1280, height: 800 },
        isMobile: false,
        hasTouch: false,
      },
    },
  ],
  webServer: {
    command: `${resolvePython()} ${path.join(root, "scripts/seed_and_serve.py")}`,
    url: `${baseURL}/login`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    cwd: repoRoot,
    env: {
      ...process.env,
      E2E_PORT: String(port),
      E2E_HOST: "127.0.0.1",
    },
  },
});
