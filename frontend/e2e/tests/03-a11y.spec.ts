import { expect, test } from "@playwright/test";
import { applySession, emergencyPasswordLogin, openAccountMenu, openMobileDrawer, vaultFilesPath } from "../helpers/auth";

test.describe("essential interface accessibility", () => {
  test("no horizontal document overflow on Overview, browser, settings or System at 375px", async ({ page }, info) => {
    test.skip(info.project.name !== "mobile-375", "375px only");
    const credentials = await emergencyPasswordLogin(page);
    async function noOverflow() { expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1); }
    await noOverflow();
    await page.goto(`/vaults/${credentials.vaults.primary.id}/settings`);
    await expect(page.locator('[data-panel="retention"]')).toBeVisible(); await noOverflow();
    await page.goto("/system"); await expect(page.getByRole("heading", { level: 1, name: /^system$/i })).toBeVisible(); await noOverflow();
    await page.goto("/"); await expect(page.getByTestId("overview-cards")).toBeVisible(); await noOverflow();
  });
  test("phone menu traps focus, has 44px targets and the fixed destinations; Escape closes it", async ({ page }, info) => {
    test.skip(info.project.name !== "mobile-375", "Phone menu only");
    await emergencyPasswordLogin(page); await openMobileDrawer(page);
    const dialog = page.getByRole("dialog");
    for (const name of [/^overview$/i, /^files$/i, /^settings$/i, /^problems/i, /^system$/i]) await expect(dialog.getByRole("link", { name })).toBeVisible();
    await expect(dialog.getByRole("button", { name: /^cloud only$/i })).toHaveCount(0);
    const tooSmall = await dialog.evaluate((root) => Array.from(root.querySelectorAll<HTMLElement>("button,a[href],select")).filter((node) => {
      const rect = node.getBoundingClientRect(); return rect.width && rect.height && (rect.width < 44 || rect.height < 44);
    }).map((node) => node.textContent));
    expect(tooSmall).toEqual([]);
    await page.keyboard.press("Tab"); expect(await dialog.evaluate((root) => root.contains(document.activeElement))).toBe(true);
    await page.keyboard.press("Escape"); await expect(dialog).toBeHidden();
  });
  test("account menu returns focus, sidebar stays outside header on desktop", async ({ page }) => {
    await emergencyPasswordLogin(page);
    const trigger = page.getByRole("button", { name: /open owner menu/i });
    await openAccountMenu(page); await page.keyboard.press("Escape"); await expect(trigger).toBeFocused();
    await expect(page.getByRole("banner").getByRole("navigation")).toHaveCount(0);
    await expect(page.getByTestId("notification-bell")).toBeVisible();
  });
  test("skip link is first and focuses main", async ({ page }) => {
    const credentials = await applySession(page.context()); await page.goto(vaultFilesPath(credentials.vaults.primary.id)); await expect(page.getByTestId("file-browser")).toBeVisible();
    const first = page.locator("a,button,input,select,[tabindex]:not([tabindex='-1'])").first(); await expect(first).toHaveClass(/skip-link/);
    await first.focus(); await page.keyboard.press("Enter"); await expect(page.locator("#main-content")).toBeFocused();
  });
});
