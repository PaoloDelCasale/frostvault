import {
  expect,
  test,
  type ElementHandle,
  type Page,
} from "@playwright/test";

import { applySession } from "../helpers/auth";

async function assertFirstFocusableIsSkipLink(page: Page): Promise<void> {
  const firstFocusable = await page.evaluate(() => {
    const candidates = Array.from(
      document.querySelectorAll<HTMLElement>(
        "a[href], button, input, select, textarea, [tabindex]",
      ),
    );
    return candidates.find((element) => {
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return (
        element.tabIndex >= 0 &&
        !element.hasAttribute("disabled") &&
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        rect.width > 0 &&
        rect.height > 0
      );
    })?.className;
  });
  expect(firstFocusable).toContain("skip-link");
}

async function assertLandmarkIsStable(
  page: Page,
  mainContent: ElementHandle<HTMLElement | SVGElement> | null,
): Promise<void> {
  await expect(page.locator("#main-content")).toHaveCount(1);
  await expect(page.locator("#main-content")).toBeVisible();
  if (!mainContent) throw new Error("main landmark was not mounted");
  const sameElement = await page
    .locator("#main-content")
    .evaluate((element, original) => element === original, mainContent);
  expect(sameElement).toBe(true);
}

async function assertNoPriorAuthenticatedShell(
  page: Page,
  priorVaultName: string,
): Promise<void> {
  await expect(
    page.getByRole("heading", { name: priorVaultName, exact: true }),
  ).toHaveCount(0);
  await expect(page.getByText(priorVaultName, { exact: true })).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: /^vault$/i })).toHaveCount(0);
  await expect(page.getByRole("navigation", { name: /vault navigation/i })).toHaveCount(0);
  await expect(page.getByTestId("notification-bell")).toHaveCount(0);

  for (const name of [
    /new vault/i,
    /manage access/i,
    /administration/i,
    /refresh list/i,
    /aggiorna elenco/i,
    /^sign out$/i,
  ]) {
    await expect(page.getByRole("button", { name })).toHaveCount(0);
  }
  // Manual Refresh list was removed (#227); catalog updates are event-driven.
  await expect(page.getByText(/refresh list/i)).toHaveCount(0);
  await expect(page.getByText(/aggiorna elenco/i)).toHaveCount(0);
  await expect(page.getByTestId("file-browser")).toHaveCount(0);
}

test.describe("auth reconciliation shell safety", () => {
  test("keeps the landmark usable and hides stale Vault controls during real Worker invalidation", async ({
    page,
    context,
  }, testInfo) => {
    await applySession(context, "admin");
    await page.goto("/");
    await expect(page.getByTestId("file-browser")).toBeVisible();
    await expect(page.locator("header h1")).toHaveText(/Archive/);
    const priorVaultName = await page.locator("header h1").innerText();
    const expectedFileName =
      priorVaultName === "Secondary Archive" ? "hello.txt" : "note.txt";
    await expect(
      page.getByRole("heading", { name: priorVaultName, exact: true }),
    ).toBeVisible();
    // Ordinary archive chrome must not expose the retired manual refresh action.
    await expect(
      page.getByRole("button", { name: /refresh list|aggiorna elenco/i }),
    ).toHaveCount(0);

    const mainContent = await page.locator("#main-content").elementHandle();
    await assertFirstFocusableIsSkipLink(page);

    let authorityRequestedResolve!: () => void;
    const authorityRequested = new Promise<void>((resolve) => {
      authorityRequestedResolve = resolve;
    });
    let releaseAuthority!: () => void;
    let authorityReleased = false;
    const authorityRelease = new Promise<void>((resolve) => {
      releaseAuthority = () => {
        authorityReleased = true;
        resolve();
      };
    });

    // Catalog discovery is event-driven (#227). Hold the SSE subscription so
    // the SPA remains on the archive surface while authority is revoked, without
    // a manual Refresh list click or /api/scan toast.
    await page.route("**/api/catalog/events**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: `event: hello\ndata: {"vault_id":1,"revision":0}\n\n`,
        headers: {
          "Cache-Control": "no-store",
          Connection: "keep-alive",
        },
      });
    });

    let initiator: Page | undefined;
    try {
      if (testInfo.project.name === "mobile-375") {
        await page.getByRole("button", { name: /open navigation/i }).click();
        await expect(page.getByRole("dialog")).toBeVisible();
        await expect(
          page.getByRole("button", { name: /refresh list|aggiorna elenco/i }),
        ).toHaveCount(0);
      }

      // Use a real same-origin Worker client without mounting another App.
      // Loading the precached manifest gives this page a Service Worker
      // controller without adding a second reconciliation loop.
      initiator = await context.newPage();
      await initiator.goto("/manifest.webmanifest");
      await initiator.waitForFunction(() => Boolean(navigator.serviceWorker?.controller));

      await page.route("**/api/me", async (route) => {
        if (!authorityReleased) {
          authorityRequestedResolve();
          await authorityRelease;
        }
        try {
          await route.continue();
        } catch (error) {
          if (!(error instanceof Error && /already handled/i.test(error.message))) {
            throw error;
          }
        }
      });

      // Register a fresh real controller. Its activate/clientsClaim path emits
      // controllerchange, which the observer handles as unknown authority and
      // reconciles through a fresh /api/me before it can render controls again.
      await initiator.evaluate(async () => {
        const registration = await navigator.serviceWorker.register(
          `/sw.js?e2e-controller=${crypto.randomUUID()}`,
          { scope: "/" },
        );
        await navigator.serviceWorker.ready;
        if (!registration.active) {
          throw new Error("Replacement Service Worker did not activate");
        }
      });

      await authorityRequested;
      await expect(page.getByRole("dialog")).toBeHidden();
      await assertNoPriorAuthenticatedShell(page, priorVaultName);
      await assertLandmarkIsStable(page, mainContent);
      await assertFirstFocusableIsSkipLink(page);
      await page.locator("a.skip-link").focus();
      await page.keyboard.press("Enter");
      await expect(page.locator("#main-content")).toBeFocused();

      releaseAuthority();
      await expect(
        page.getByRole("heading", { name: priorVaultName, exact: true }),
      ).toBeVisible();
      await expect(
        page.getByText(expectedFileName).locator("visible=true").first(),
      ).toBeVisible();
      await assertFirstFocusableIsSkipLink(page);
      await expect(
        page.getByRole("button", { name: /refresh list|aggiorna elenco/i }),
      ).toHaveCount(0);
    } finally {
      releaseAuthority();
      await page.unroute("**/api/me");
      await page.unroute("**/api/catalog/events**");
      await initiator?.close();
    }
  });
});
