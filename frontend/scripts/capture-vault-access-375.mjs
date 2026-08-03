/**
 * Capture 375px screenshots of each vault-access panel for the PR.
 * Run: node scripts/capture-vault-access-375.mjs
 */
import { createServer } from "node:http";
import { readFileSync, existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = path.dirname(fileURLToPath(import.meta.url));
const dist = path.resolve(root, "../dist");
const artifacts = path.resolve(root, "../artifacts");
mkdirSync(artifacts, { recursive: true });

const mime = {
  ".html": "text/html",
  ".js": "application/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
  ".png": "image/png",
};

const server = createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://127.0.0.1");
  let filePath = path.join(dist, url.pathname === "/" ? "index.html" : url.pathname);
  if (!existsSync(filePath) || !filePath.startsWith(dist)) {
    filePath = path.join(dist, "index.html");
  }
  const ext = path.extname(filePath);
  res.writeHead(200, { "Content-Type": mime[ext] ?? "text/plain" });
  res.end(readFileSync(filePath));
});

await new Promise((resolve) => server.listen(4177, "127.0.0.1", resolve));

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 375, height: 812 } });
page.on("console", (message) => {
  if (message.type() === "error") console.error("browser:", message.text());
});
page.on("pageerror", (error) => console.error("browser page error:", error.message));

const en = JSON.parse(
  readFileSync(path.resolve(root, "../../app/locales/en.json"), "utf8"),
);

await page.route("**/api/**", async (route) => {
  const req = route.request();
  const url = req.url();
  const method = req.method();
  if (url.includes("/api/i18n/catalog")) {
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ locale: "en", locales: ["en", "it"], messages: en }),
    });
  }
  if (url.includes("/api/me")) {
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: 1,
        username: "owner",
        display_name: "Owner",
        is_admin: true,
        active: true,
        session_version: 1,
        csrf_token: "csrf",
        auth_method: "local",
        locale: "en",
        locales: ["en", "it"],
        vault: {
          id: 1,
          slug: "test",
          name: "Test Archive",
          role: "owner",
          can_operate: true,
          delete_enabled: true,
          cloud_deletion_enabled: false,
          is_vault_owner: true,
        },
      }),
    });
  }
  if (url.includes("/api/storage-classes")) {
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    });
  }
  if (url.includes("/api/vault/members") && method === "GET") {
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          { id: 1, username: "owner", display_name: "Owner One", role: "owner" },
          { id: 2, username: "bob", display_name: "Bob Operator", role: "operator" },
        ],
      }),
    });
  }
  if (url.includes("/api/vault/quotas")) {
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        limits: {
          storage_soft_limit_bytes: 1000,
          storage_hard_limit_bytes: 2000,
          concurrency_soft_limit: 2,
          concurrency_hard_limit: 4,
          restore_30d_soft_limit_bytes: null,
          restore_30d_hard_limit_bytes: null,
        },
        usage: { storage_bytes: 500, concurrency: 1, restore_30d_bytes: 0 },
        evaluation: {
          state: "evaluated",
          allowed: true,
          decisions: [{ code: "quota.storage.soft_exceeded", severity: "warning" }],
        },
      }),
    });
  }
  if (url.includes("/api/vault/operation-policy")) {
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        auto_upload: true,
        auto_local_cleanup: true,
        local_retention_days: 45,
        stability_seconds: 300,
        include_globs: ["**/*.txt"],
        exclude_globs: ["tmp/**"],
        bandwidth_limit_kibps: null,
        operating_windows: [],
      }),
    });
  }
  if (url.includes("/api/vault/lifecycle")) {
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        default_policy_id: null,
        folder_overrides: [
          { folder_path: "photos/2024", policy_id: 2 },
        ],
        policies: [
          {
            id: 2,
            name: "Photos custom ladder",
            profile: {
              transitions: [
                { days: 30, storage_class: "STANDARD_IA" },
                { days: 180, storage_class: "DEEP_ARCHIVE" },
              ],
              noncurrent_transitions: [],
              expiration_days: null,
              noncurrent_expiration_days: null,
            },
          },
        ],
        guided_profiles: {
          standard_only: { transitions: [] },
          ia_after_30: { transitions: [{ days: 30, storage_class: "STANDARD_IA" }] },
          archive_tiered: {
            transitions: [
              { days: 30, storage_class: "STANDARD_IA" },
              { days: 90, storage_class: "GLACIER_IR" },
              { days: 365, storage_class: "DEEP_ARCHIVE" },
            ],
            noncurrent_transitions: [
              { days: 180, storage_class: "DEEP_ARCHIVE" },
            ],
          },
        },
      }),
    });
  }
  if (url.includes("/api/vault/cloud-deletion")) {
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        enabled: false,
        delete_marker_explanation: "A Delete Marker is a reversible cloud marker.",
        accepted_single_identity_risk: "Single IAM identity risk documented.",
      }),
    });
  }
  return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
});

await page.goto("http://127.0.0.1:4177/vault/access", { waitUntil: "networkidle" });
const lifecyclePanel = page.locator('[data-panel="lifecycle"]');
await lifecyclePanel.waitFor();

async function screenshotAtViewportWidth(locator, outputPath) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  const viewport = page.viewportSize();
  if (!box || !viewport || viewport.width !== 375) {
    throw new Error("Unable to capture a 375px-wide lifecycle screenshot");
  }
  const scrollY = await page.evaluate(() => window.scrollY);
  await page.screenshot({
    path: outputPath,
    fullPage: true,
    clip: {
      x: 0,
      y: box.y + scrollY,
      width: viewport.width,
      height: box.height,
    },
  });
}

await screenshotAtViewportWidth(
  lifecyclePanel,
  path.join(artifacts, "lifecycle-guided-picker-375px.png"),
);

await page.getByLabel(/vault default profile/i).selectOption("ia_after_30");
await page.getByRole("button", { name: /customize/i }).first().click();
await page.getByRole("button", { name: /add rule/i }).click();
await page.getByLabel(/after n days from creation/i).nth(1).fill("180");
await page.getByLabel(/target storage class/i).nth(1).selectOption("DEEP_ARCHIVE");
await screenshotAtViewportWidth(
  page.locator("[data-lifecycle-editor]"),
  path.join(artifacts, "lifecycle-custom-two-rules-375px.png"),
);

const currentDays = page.getByLabel(/after n days from creation/i);
await currentDays.nth(1).fill("20");
await page.getByLabel(/target storage class/i).nth(1).selectOption("ONEZONE_IA");
await page.getByRole("button", { name: /save custom rules/i }).click();
await screenshotAtViewportWidth(
  page.locator("[data-lifecycle-editor]"),
  path.join(artifacts, "lifecycle-validation-error-375px.png"),
);

await page.getByRole("button", { name: /^cancel$/i }).click();
await lifecyclePanel.locator("text=photos/2024").scrollIntoViewIfNeeded();
await screenshotAtViewportWidth(
  lifecyclePanel,
  path.join(artifacts, "lifecycle-custom-folder-override-375px.png"),
);

const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
if (overflow > 0) throw new Error(`375px layout overflows horizontally by ${overflow}px`);

const panels = [
  "add-member",
  "members",
  "quotas",
  "retention",
  "operation-policy",
  "lifecycle",
  "cloud-deletion",
];

for (const panel of panels) {
  const locator = page.locator(`[data-panel="${panel}"]`);
  await locator.scrollIntoViewIfNeeded();
  await page.waitForTimeout(150);
  await locator.screenshot({
    path: path.join(artifacts, `vault-access-${panel}-375px.png`),
  });
}

// Full page scroll for overview
await page.screenshot({
  path: path.join(artifacts, "vault-access-375px.png"),
  fullPage: true,
});

// Transfer dialog
await page.getByRole("button", { name: /transfer ownership/i }).click();
await page.getByRole("dialog").waitFor();
await page.screenshot({
  path: path.join(artifacts, "vault-access-transfer-375px.png"),
});

await browser.close();
server.close();
console.log("Wrote screenshots to", artifacts);
