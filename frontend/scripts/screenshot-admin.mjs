/**
 * Manual 375px screenshots for issue #69 (admin page + members sections).
 * Serves frontend/dist via vite preview and injects a stubbed /api layer.
 */
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const artifacts = path.join(root, "artifacts");
const en = JSON.parse(
  readFileSync(path.join(root, "../app/locales/en.json"), "utf8"),
);

const users = [
  {
    id: 10,
    display_name: "Ada Admin",
    username: "ada",
    active: true,
    is_admin: true,
    vault_count: 2,
  },
  {
    id: 20,
    display_name: "Owen Operator",
    username: "owen",
    active: true,
    is_admin: false,
    vault_count: 1,
  },
];

const vaults = [
  {
    id: 1,
    name: "Family Archive",
    slug: "family",
    source_root: "/sources/family",
    s3_prefix: "vaults/family/",
    enabled: true,
    member_count: 2,
    encryption_mode: "crypt",
  },
];

const members = {
  items: [
    {
      id: 10,
      display_name: "Ada Admin",
      username: "ada",
      active: true,
      role: "owner",
    },
    {
      id: 20,
      display_name: "Owen Operator",
      username: "owen",
      active: true,
      role: "operator",
    },
  ],
};

const quotas = {
  vault_id: 1,
  limits: {
    storage_soft_limit_bytes: 1000,
    storage_hard_limit_bytes: 2000,
    concurrency_soft_limit: null,
    concurrency_hard_limit: null,
    restore_30d_soft_limit_bytes: null,
    restore_30d_hard_limit_bytes: null,
  },
  usage: { storage_bytes: 400, concurrency: 1, restore_30d_bytes: 0 },
  evaluation: {
    state: "evaluated",
    allowed: true,
    decisions: [
      { code: "quota.storage.soft_exceeded", severity: "warning" },
    ],
  },
};

async function main() {
  const preview = spawn(
    "npx",
    ["vite", "preview", "--host", "127.0.0.1", "--port", "5179"],
    { cwd: root, stdio: "pipe" },
  );
  await new Promise((resolve) => setTimeout(resolve, 1500));

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 375, height: 812 } });

  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const pathName = url.pathname;
    const method = req.method();

    if (pathName.startsWith("/api/i18n/catalog")) {
      return route.fulfill({
        json: { locale: "en", locales: ["en", "it"], messages: en },
      });
    }
    if (pathName === "/api/me") {
      return route.fulfill({
        json: {
          id: 10,
          username: "ada",
          display_name: "Ada Admin",
          is_admin: true,
          active: true,
          session_version: 1,
          csrf_token: "csrf",
          auth_method: "local",
          locale: "en",
          locales: ["en", "it"],
          vault: null,
        },
      });
    }
    if (pathName === "/api/admin/users" && method === "GET") {
      return route.fulfill({ json: { items: users } });
    }
    if (pathName === "/api/admin/vaults" && method === "GET") {
      return route.fulfill({ json: { items: vaults } });
    }
    if (pathName === "/api/admin/vaults/1/members" && method === "GET") {
      return route.fulfill({ json: members });
    }
    if (pathName === "/api/admin/vaults/1/quotas" && method === "GET") {
      return route.fulfill({ json: quotas });
    }
    return route.fulfill({ status: 404, json: { error: "not mocked" } });
  });

  await page.goto("http://127.0.0.1:5179/admin", { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: /users and vaults/i }).waitFor();
  await page.screenshot({
    path: path.join(artifacts, "admin-375px.png"),
    fullPage: true,
  });

  // Open members via bottom sheet (375px)
  await page.getByRole("button", { name: /actions/i }).last().click();
  await page.getByRole("button", { name: /manage access/i }).click();
  const dialog = page.getByRole("dialog");
  await dialog.waitFor();

  await page.screenshot({
    path: path.join(artifacts, "admin-members-375px.png"),
  });

  await page.getByRole("heading", { name: /vault quotas/i }).scrollIntoViewIfNeeded();
  await page.screenshot({
    path: path.join(artifacts, "admin-quotas-375px.png"),
  });

  await page.getByRole("heading", { name: /assign operator/i }).scrollIntoViewIfNeeded();
  await page.screenshot({
    path: path.join(artifacts, "admin-assign-375px.png"),
  });

  await page.getByRole("heading", { name: /transfer primary ownership/i }).scrollIntoViewIfNeeded();
  await page.screenshot({
    path: path.join(artifacts, "admin-transfer-375px.png"),
  });

  await page.getByRole("heading", { name: /recovery export/i }).scrollIntoViewIfNeeded();
  await page.screenshot({
    path: path.join(artifacts, "admin-recovery-375px.png"),
  });

  await browser.close();
  preview.kill("SIGTERM");
  console.log("Wrote admin 375px screenshots to frontend/artifacts/");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
