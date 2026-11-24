import { expect, test } from "@playwright/test";

import { applySession, vaultFilesPath } from "../helpers/auth";

test("the Session names no Vault: each page keeps the Vault of its own route (#805)", async ({ page, context }) => {
  const credentials = await applySession(context);
  const me = await (await page.request.get("/api/me")).json();
  expect(me).not.toHaveProperty("vault");
  const removed = await page.request.post("/api/vaults/select", {
    headers: { "X-CSRF-Token": me.csrf_token }, data: { vault_id: credentials.vaults.secondary.id },
  });
  expect(removed.status()).toBe(404);
  // The sidebar is collapsed on phones, so read the link without requiring visibility.
  const files = page.locator('aside nav a[href$="/files"]');
  // The Overview belongs to no Vault: its sidebar names none (#677).
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(files).toHaveCount(0);
  // A Vault page keeps its own Vault after a reload.
  for (const vault of [credentials.vaults.secondary, credentials.vaults.primary]) {
    await page.goto(vaultFilesPath(vault.id));
    await expect(files).toHaveAttribute("href", vaultFilesPath(vault.id));
    await page.reload();
    await expect(files).toHaveAttribute("href", vaultFilesPath(vault.id));
  }
});

test("an existing PWA installation is retired without clearing Owner preferences", async ({ page, context }) => {
  const credentials = await applySession(context);
  await page.goto(vaultFilesPath(credentials.vaults.primary.id));
  await expect(page.getByTestId("file-browser")).toBeVisible();
  // A Worker's entry fetch is not page-route interceptable in Chromium.
  // The disposable CI server serves this fixture; the normal URL still serves
  // the real retirement Worker shipped with the application.
  const legacyScript = await page.request.get("/sw.js?legacy-test");
  expect(legacyScript.ok()).toBe(true);
  expect(await legacyScript.text()).toContain("legacy-worker-fixture");
  await page.evaluate(async () => {
    await navigator.serviceWorker.register("/sw.js?legacy-test", { scope: "/" });
    await navigator.serviceWorker.ready;
    await (await caches.open("frostvault-file-listing-v3:old-session")).put("/api/files", new Response("old listing"));
    localStorage.setItem("frostvault.files.cache.v3:old-session", "old listing");
    localStorage.setItem("frostvault.theme.owner", "dark");
  });
  await page.goto(vaultFilesPath(credentials.vaults.primary.id));
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
  const credentials = await applySession(context);
  await page.goto(vaultFilesPath(credentials.vaults.primary.id));
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
  await page.route("**/api/vaults/*/files?**", (route) => route.abort("internetdisconnected"));
  await page.reload();
  await expect(page.getByTestId("file-browser")).toBeVisible();
  await expect(page.getByTestId("file-list-error")).toBeVisible();
  await expect(page.getByTestId("offline-shell")).toHaveCount(0);
  await expect(page.getByTestId("offline-stale-banner")).toHaveCount(0);
});
