import { expect, test } from "@playwright/test";

import { applySession } from "../helpers/auth";

test("another client's Session selection cannot redirect this browser's pinned Vault",  async ({ page, context }) => {
  await applySession(context);
  await page.goto("/");
  await expect(page.getByTestId("file-browser")).toBeVisible();
  const me = await (await page.request.get("/api/me")).json();
  const vaults = await (await page.request.get("/api/vaults")).json();
  const other = vaults.items.find((vault: { id: number; name: string }) => vault.id !== me.vault.id);
  if (!other) throw new Error("This test requires the two seeded Vaults");
  const select = (vaultId: number) => page.request.post("/api/vaults/select", {
    headers: { "X-CSRF-Token": me.csrf_token }, data: { vault_id: vaultId },
  });
  try {
    const response = await select(other.id);
    expect(response.ok()).toBe(true);
    await page.reload();
    await expect(page.getByRole("heading", { level: 1, name: me.vault.name, exact: true })).toBeVisible();
    await expect(page.getByTestId("file-browser")).toBeVisible();
  } finally {
    const response = await select(me.vault.id);
    expect(response.ok()).toBe(true);
  }
});

test("an existing PWA installation is retired without clearing Owner preferences", async ({ page, context }) => {
  await applySession(context);
  await page.goto("/");
  await expect(page.getByTestId("file-browser")).toBeVisible();
  await page.route("**/sw.js?legacy-test", (route) => route.fulfill({
    contentType: "application/javascript",
    body: 'self.addEventListener("install", e => e.waitUntil(self.skipWaiting())); self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));',
  }));
  await page.evaluate(async () => {
    await navigator.serviceWorker.register("/sw.js?legacy-test", { scope: "/" });
    await navigator.serviceWorker.ready;
    await (await caches.open("frostvault-file-listing-v3:old-session")).put("/api/files", new Response("old listing"));
    localStorage.setItem("frostvault.files.cache.v3:old-session", "old listing");
    localStorage.setItem("frostvault.theme.owner", "dark");
  });
  await page.goto("/");
  await expect(page.getByTestId("file-browser")).toBeVisible();
  await expect.poll(() => page.evaluate(async () => (await navigator.serviceWorker.getRegistrations()).length)).toBe(0);
  await expect.poll(() => page.evaluate(async () => (await caches.keys()).length)).toBe(0);
  expect(await page.evaluate(() => localStorage.getItem("frostvault.files.cache.v3:old-session"))).toBeNull();
  expect(await page.evaluate(() => localStorage.getItem("frostvault.theme.owner"))).toBe("dark");
  await page.reload();
  await expect(page.getByTestId("file-browser")).toBeVisible();
  expect(await page.evaluate(() => navigator.serviceWorker.controller)).toBeNull();
});

test("the responsive site has no offline shell, registered Worker or persisted listings", async ({
  page,
  context,
}) => {
  await applySession(context);
  await page.goto("/");
  await expect(page.getByTestId("file-browser")).toBeVisible();
  await expect(page.locator("#main-content")).toBeVisible();
  await page.locator("a.skip-link").focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();

  const browserState = await page.evaluate(async () => ({
    registrations: (await navigator.serviceWorker.getRegistrations()).length,
    controller: navigator.serviceWorker.controller?.scriptURL ?? null,
    manifests: document.querySelectorAll('link[rel="manifest"]').length,
    persistedListings: Object.keys(localStorage).filter((key) => key.startsWith("frostvault.files.cache.")),
    caches: await caches.keys(),
  }));
  expect(browserState).toEqual({
    registrations: 0,
    controller: null,
    manifests: 0,
    persistedListings: [],
    caches: [],
  });

  // A new document must use the API, never a saved listing from a prior load.
  await page.route("**/api/files?**", (route) => route.abort("internetdisconnected"));
  await page.reload();
  await expect(page.getByTestId("file-browser")).toBeVisible();
  await expect(page.getByTestId("file-list-error")).toBeVisible();
  await expect(page.getByTestId("offline-shell")).toHaveCount(0);
  await expect(page.getByTestId("offline-stale-banner")).toHaveCount(0);
});
