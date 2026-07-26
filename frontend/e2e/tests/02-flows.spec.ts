import { expect, test, type Page } from "@playwright/test";

import { applySession, breakGlassLogin, openMobileDrawer } from "../helpers/auth";

async function openDirectory(page: Page, name: string) {
  // Cards and table both render; prefer the visible card/table row button.
  const target = page
    .locator(
      `[data-testid="file-list-cards"] button:has-text("${name}"), [data-testid="file-list-table"] button:has-text("${name}")`,
    )
    .first();
  await expect(target).toBeVisible();
  await target.click();
}

test.describe("archive flows", () => {
  test("flow 2 — navigate directories, breadcrumbs, Up, browser back", async ({
    page,
  }) => {
    await breakGlassLogin(page);
    await openDirectory(page, "reports");
    await expect(page).toHaveURL(/directory=reports/);
    await expect(page.getByText("readme.txt").first()).toBeVisible();

    await expect(page.getByTestId("breadcrumbs")).toBeVisible();
    await page.getByTestId("up-directory").click();
    await expect(page).not.toHaveURL(/directory=reports/);
    await expect(page.getByText("reports").first()).toBeVisible();

    await openDirectory(page, "reports");
    await page.goBack();
    await expect(page).not.toHaveURL(/directory=reports/);
  });

  test("flow 3 — search and state filter update the URL", async ({ page }) => {
    await breakGlassLogin(page);
    await page.getByTestId("file-search").fill("readme");
    await expect(page).toHaveURL(/q=readme/, { timeout: 5_000 });
    await expect(page.getByText("readme.txt").first()).toBeVisible();

    await page.getByTestId("state-filter").selectOption("both");
    await expect(page).toHaveURL(/state=both/);
  });

  test("flow 4 — open a Vault File and read Path History", async ({ page }) => {
    await breakGlassLogin(page);
    await openDirectory(page, "reports");
    const file = page
      .locator(
        '[data-testid="file-list-cards"] button:has-text("readme.txt"), [data-testid="file-list-table"] button:has-text("readme.txt")',
      )
      .first();
    await file.click();
    await expect(page.getByTestId("path-history")).toBeVisible();
    await expect(page.getByTestId("path-history-timeline")).toContainText(
      "old-readme.txt",
    );
  });

  test("flow 5 — cancel a destructive confirmation without side effects", async ({
    page,
  }) => {
    await breakGlassLogin(page);
    const more = page
      .locator(
        '[data-testid="more-actions-note.txt"], [data-testid="desktop-actions-note.txt"] button',
      )
      .first();
    await more.click();

    // Mobile opens a bottom sheet; desktop may have inline actions.
    const freeSpace = page
      .getByRole("button", { name: /free local space|libera spazio/i })
      .first();
    await expect(freeSpace).toBeVisible();
    await freeSpace.click();

    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /cancel|annulla/i }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText("note.txt").first()).toBeVisible();
  });

  test("flow 6 — switch vault from the drawer/nav", async ({ page }) => {
    await breakGlassLogin(page);
    await openMobileDrawer(page);
    const vaultSelect = page.getByLabel(/^vault$/i).first();
    await expect(vaultSelect).toBeVisible();
    await vaultSelect.selectOption({ label: "Secondary Archive" });
    await expect(page.getByRole("heading", { name: /secondary archive/i })).toBeVisible();
    await expect(page.getByText("hello.txt").first()).toBeVisible();
  });

  test("flow 7 — switch locale to Italian", async ({ page }) => {
    await breakGlassLogin(page);
    await openMobileDrawer(page);
    const language = page.getByLabel(/^language$/i).first();
    await language.selectOption("it");
    // Close the drawer on mobile so the archive search field is visible.
    const close = page.getByRole("button", { name: /close navigation/i });
    if (await close.isVisible()) await close.click();
    await expect(page.getByTestId("file-search")).toHaveAttribute(
      "placeholder",
      /cerca per nome/i,
    );
  });

  test("flow 8 — owner reaches /vault/access and edits a quota", async ({
    page,
  }) => {
    await breakGlassLogin(page);
    await openMobileDrawer(page);
    await page.getByRole("button", { name: /manage access|gestisci accesso/i }).click();
    await expect(page).toHaveURL(/\/vault\/access/);
    await expect(page.locator('[data-panel="quotas"]')).toBeVisible();

    await page.locator('input[name="storage_soft_limit_bytes"]').fill("1000");
    await page.locator('input[name="reason"]').fill("e2e quota tweak");
    await page.getByRole("button", { name: /save quotas|salva quote/i }).click();
    await expect(page.getByText(/quotas updated|quote del vault aggiornate/i)).toBeVisible();
  });

  test("flow 9 — admin opens /admin members dialog and closes it", async ({
    page,
  }) => {
    await breakGlassLogin(page);
    await openMobileDrawer(page);
    await page.getByRole("button", { name: /administration|amministrazione/i }).click();
    await expect(page).toHaveURL(/\/admin/);

    const manage = page.getByRole("button", { name: /manage access|gestisci accesso/i }).first();
    // On mobile the members entry may be behind a row ⋯ sheet.
    if (!(await manage.isVisible())) {
      await page.getByRole("button", { name: /row actions|azioni/i }).first().click();
      await page.getByRole("button", { name: /manage access|gestisci accesso/i }).click();
    } else {
      await manage.click();
    }
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toBeHidden();
  });

  test("flow 10 — viewer has no operational actions", async ({ page, context }) => {
    await applySession(context, "viewer");
    await page.goto("/");
    await expect(page.getByTestId("file-browser")).toBeVisible();
    await expect(page.getByText("note.txt").first()).toBeVisible();
    await expect(
      page.locator('[data-testid="more-actions-note.txt"]'),
    ).toHaveCount(0);
    await expect(
      page.locator('[data-testid="desktop-actions-note.txt"]'),
    ).toHaveCount(0);
  });

  test("flow 11 — sign out", async ({ page }) => {
    await breakGlassLogin(page);
    await openMobileDrawer(page);
    await page.getByRole("button", { name: /^sign out$|^esci$/i }).click();
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByLabel(/username/i)).toBeVisible();
  });
});
