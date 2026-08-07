import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  applySession,
  breakGlassLogin,
  openAccountMenu,
  openMobileDrawer,
} from "../helpers/auth";

async function assertNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => {
    const doc = document.documentElement;
    return {
      scrollWidth: doc.scrollWidth,
      clientWidth: doc.clientWidth,
    };
  });
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1);
}

async function firstFocusable(page: Page): Promise<Locator> {
  return page
    .locator("a, button, input, select, textarea, [tabindex]:not([tabindex='-1'])")
    .first();
}

test.describe("accessibility and touch", () => {
  test("no horizontal document scroll at 375px on key pages", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile-375", "375px only");
    await breakGlassLogin(page);
    await assertNoHorizontalOverflow(page);

    await page.goto("/vault/access");
    await expect(page.locator('[data-panel="quotas"]')).toBeVisible();
    await assertNoHorizontalOverflow(page);

    await page.goto("/admin");
    await expect(page.getByText("Family Archive").first()).toBeVisible();
    await assertNoHorizontalOverflow(page);
  });

  test("interactive elements in the vault drawer are at least 44×44 on mobile", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile-375", "375px only");
    await breakGlassLogin(page);
    await openMobileDrawer(page);
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel(/^vault$/i)).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: /manage access|gestisci accesso/i }),
    ).toBeVisible();
    const tooSmall = await dialog.evaluate((root) => {
      const nodes = Array.from(
        root.querySelectorAll<HTMLElement>("button, select, a[href], [role='button']"),
      );
      const bad: string[] = [];
      for (const el of nodes) {
        const style = window.getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden") continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        if (rect.width < 44 || rect.height < 44) {
          const label =
            el.getAttribute("aria-label") ||
            el.textContent?.trim().slice(0, 40) ||
            el.tagName;
          bad.push(`${label} (${Math.round(rect.width)}x${Math.round(rect.height)})`);
        }
      }
      return bad;
    });
    expect(tooSmall, `drawer tap targets under 44px: ${tooSmall.join("; ")}`).toEqual(
      [],
    );
  });

  test("account menu targets are at least 44×44 and Esc returns focus", async ({
    page,
  }) => {
    await breakGlassLogin(page);
    const trigger = page.getByRole("button", {
      name: /open account menu|apri menu account/i,
    });
    await openAccountMenu(page);
    const dialog = page.getByRole("dialog", { name: /^account$/i });
    await expect(
      dialog.getByRole("button", { name: /new vault|nuovo vault/i }),
    ).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: /sign out|esci/i }),
    ).toBeVisible();
    const tooSmall = await dialog.evaluate((root) => {
      const nodes = Array.from(
        root.querySelectorAll<HTMLElement>("button, select, a[href], [role='button']"),
      );
      const bad: string[] = [];
      for (const el of nodes) {
        const style = window.getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden") continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        if (rect.width < 44 || rect.height < 44) {
          const label =
            el.getAttribute("aria-label") ||
            el.textContent?.trim().slice(0, 40) ||
            el.tagName;
          bad.push(`${label} (${Math.round(rect.width)}x${Math.round(rect.height)})`);
        }
      }
      return bad;
    });
    expect(
      tooSmall,
      `account menu tap targets under 44px: ${tooSmall.join("; ")}`,
    ).toEqual([]);
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(trigger).toBeFocused();
  });

  test("desktop header stays a single non-wrapping control row for owner/admin", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1280", "desktop layout only");
    await breakGlassLogin(page);
    await expect(page.getByTestId("file-browser")).toBeVisible();

    const metrics = await page.evaluate(() => {
      const row = document.querySelector(
        '[data-testid="app-shell-header-row"]',
      ) as HTMLElement | null;
      if (!row) return null;
      const style = window.getComputedStyle(row);
      const children = Array.from(row.children) as HTMLElement[];
      const tops = children
        .filter((el) => el.getBoundingClientRect().height > 0)
        .map((el) => Math.round(el.getBoundingClientRect().top));
      return {
        flexWrap: style.flexWrap,
        scrollWidth: row.scrollWidth,
        clientWidth: row.clientWidth,
        tops,
        uniqueTops: Array.from(new Set(tops)),
      };
    });

    expect(metrics).not.toBeNull();
    expect(metrics!.flexWrap).toBe("nowrap");
    expect(metrics!.scrollWidth).toBeLessThanOrEqual(metrics!.clientWidth + 1);
    expect(metrics!.uniqueTops.length).toBe(1);

    // Secondary destinations stay behind the account menu, not permanent header buttons.
    const header = page.getByRole("banner");
    await expect(
      header.getByRole("button", { name: /^new vault$|^nuovo vault$/i }),
    ).toHaveCount(0);
    await expect(
      header.getByRole("button", { name: /^administration$|^amministrazione$/i }),
    ).toHaveCount(0);
    await expect(
      header.getByRole("button", { name: /^sign out$|^esci$/i }),
    ).toHaveCount(0);
    await expect(header.getByTestId("notification-bell")).toBeVisible();
    await expect(
      header.getByRole("button", {
        name: /open account menu|apri menu account/i,
      }),
    ).toBeVisible();
    await expect(
      header.getByRole("navigation", { name: /vault navigation/i }),
    ).toBeVisible();
  });

  test("drawer traps focus and Esc closes it", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile-375", "drawer is mobile-only");
    await breakGlassLogin(page);
    await openMobileDrawer(page);
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await page.keyboard.press("Tab");
    const focusedInside = await page.evaluate(() => {
      const active = document.activeElement;
      const dialogEl = document.querySelector('[role="dialog"]');
      return Boolean(active && dialogEl && dialogEl.contains(active));
    });
    expect(focusedInside).toBe(true);
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  });

  test("skip link is the first focusable element", async ({ page }) => {
    await applySession(page.context(), "admin");
    await page.goto("/");
    await expect(page.getByTestId("file-browser")).toBeVisible();
    const first = await firstFocusable(page);
    await expect(first).toHaveClass(/skip-link/);
    await first.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#main-content")).toBeFocused();
  });
});
