import { expect, test } from "@playwright/test";

import { applySession } from "../helpers/auth";

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
