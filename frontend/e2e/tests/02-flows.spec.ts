import { expect, test, type Page } from "@playwright/test";

import { applySession, breakGlassLogin, openMobileDrawer } from "../helpers/auth";

function visibleFileButton(page: Page, name: string) {
  return page
    .locator(
      `[data-testid="file-list-cards"] button:has-text("${name}"), [data-testid="file-list-table"] button:has-text("${name}")`,
    )
    .locator("visible=true")
    .first();
}

async function openDirectory(page: Page, name: string) {
  const target = visibleFileButton(page, name);
  await expect(target).toBeVisible();
  await target.click();
}

async function vaultSelect(page: Page) {
  const drawer = page.getByRole("dialog");
  if (await drawer.isVisible()) {
    return drawer.getByLabel(/^vault$/i);
  }
  return page.getByLabel(/^vault$/i).locator("visible=true").first();
}

async function languageSelect(page: Page) {
  const drawer = page.getByRole("dialog");
  if (await drawer.isVisible()) {
    return drawer.getByLabel(/^language$/i);
  }
  return page.getByLabel(/^language$/i).locator("visible=true").first();
}

test.describe("archive flows", () => {
  test("flow 2 — navigate directories, breadcrumbs, Up, browser back", async ({
    page,
  }) => {
    await breakGlassLogin(page);
    await openDirectory(page, "reports");
    await expect(page).toHaveURL(/directory=reports/);
    await expect(page.getByText("readme.txt").locator("visible=true").first()).toBeVisible();

    await expect(page.getByTestId("breadcrumbs")).toBeVisible();
    await page.getByTestId("up-directory").click();
    await expect(page).not.toHaveURL(/directory=reports/);
    await expect(page.getByText("reports").locator("visible=true").first()).toBeVisible();

    await openDirectory(page, "reports");
    await page.goBack();
    await expect(page).not.toHaveURL(/directory=reports/);
  });

  test("flow 3 — search and state filter update the URL", async ({ page }) => {
    await breakGlassLogin(page);
    await page.getByTestId("file-search").fill("readme");
    await expect(page).toHaveURL(/q=readme/, { timeout: 5_000 });
    await expect(page.getByText("readme.txt").locator("visible=true").first()).toBeVisible();

    await page.getByTestId("state-filter").selectOption("both");
    await expect(page).toHaveURL(/state=both/);
  });

  test("flow 4 — open a Vault File and read Path History", async ({ page }) => {
    await breakGlassLogin(page);
    await openDirectory(page, "reports");
    await visibleFileButton(page, "readme.txt").click();
    await expect(page.getByTestId("path-history")).toBeVisible();
    await expect(page.getByTestId("path-history-timeline")).toContainText(
      "old-readme.txt",
    );
  });

  test("flow 5 — cancel a destructive confirmation without side effects", async ({
    page,
  }, testInfo) => {
    await breakGlassLogin(page);
    if (testInfo.project.name === "mobile-375") {
      await page
        .locator('[data-testid="more-actions-note.txt"]')
        .locator("visible=true")
        .click();
      await page
        .getByRole("button", { name: /free local space|libera spazio/i })
        .click();
    } else {
      await page
        .locator('[data-testid="desktop-actions-note.txt"] button[data-action="free-space"]')
        .click();
    }

    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /cancel|annulla/i }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText("note.txt").locator("visible=true").first()).toBeVisible();
  });

  test("flow 6 — switch vault from the drawer/nav", async ({ page }, testInfo) => {
    await breakGlassLogin(page);
    if (testInfo.project.name === "mobile-375") {
      await openMobileDrawer(page);
    }
    const select = await vaultSelect(page);
    await expect(select).toBeVisible();
    await select.selectOption({ label: "Secondary Archive" });
    await expect(page.getByRole("heading", { name: /secondary archive/i })).toBeVisible();
    await expect(page.getByText("hello.txt").locator("visible=true").first()).toBeVisible();
  });

  test("flow 7 — switch locale to Italian", async ({ page }, testInfo) => {
    await breakGlassLogin(page);
    if (testInfo.project.name === "mobile-375") {
      await openMobileDrawer(page);
    }
    const language = await languageSelect(page);
    await language.selectOption("it");
    if (testInfo.project.name === "mobile-375") {
      const close = page.getByRole("button", { name: /close navigation/i });
      if (await close.isVisible()) await close.click();
    }
    await expect(page.getByTestId("file-search")).toHaveAttribute(
      "placeholder",
      /cerca per nome/i,
    );
  });

  test("flow 8 — owner reaches /vault/access and edits a quota", async ({
    page,
  }, testInfo) => {
    await breakGlassLogin(page);
    if (testInfo.project.name === "mobile-375") {
      await openMobileDrawer(page);
      await page
        .getByRole("dialog")
        .getByRole("button", { name: /manage access|gestisci accesso/i })
        .click();
    } else {
      await page
        .getByRole("navigation", { name: /vault navigation/i })
        .getByRole("button", { name: /manage access|gestisci accesso/i })
        .click();
    }
    await expect(page).toHaveURL(/\/vault\/access/);
    await expect(page.locator('[data-panel="quotas"]')).toBeVisible();

    await page.locator('input[name="storage_soft_limit_bytes"]').fill("1000");
    await page.locator('input[name="reason"]').fill("e2e quota tweak");
    await page.getByRole("button", { name: /save quotas|salva quote/i }).click();
    await expect(page.getByText(/quotas updated|quote del vault aggiornate/i)).toBeVisible();
  });

  test("flow 9 — admin opens /admin members dialog and closes it", async ({
    page,
  }, testInfo) => {
    await breakGlassLogin(page);
    if (testInfo.project.name === "mobile-375") {
      await openMobileDrawer(page);
      await page
        .getByRole("dialog")
        .getByRole("button", { name: /administration|amministrazione/i })
        .click();
    } else {
      await page
        .getByRole("navigation", { name: /vault navigation/i })
        .getByRole("button", { name: /administration|amministrazione/i })
        .click();
    }
    await expect(page).toHaveURL(/\/admin/);
    await expect(page.getByText("Family Archive").first()).toBeVisible();

    if (testInfo.project.name === "mobile-375") {
      await page
        .getByRole("listitem")
        .filter({ hasText: "Family Archive" })
        .getByRole("button", { name: /^actions$|^azioni$/i })
        .click();
      await page.getByRole("button", { name: /manage access|gestisci accesso/i }).click();
    } else {
      await page
        .getByRole("button", { name: /manage access|gestisci accesso/i })
        .locator("visible=true")
        .first()
        .click();
    }
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toBeHidden();

    const sections = page.getByRole("navigation", {
      name: /administration sections|sezioni di amministrazione/i,
    });
    await sections
      .getByRole("link", { name: /users and identities|utenti e identità/i })
      .click();
    await expect(page).toHaveURL(/\/admin\/users/);
    await expect(page.getByRole("heading", { name: /invites|inviti/i })).toBeVisible();

    await sections
      .getByRole("link", { name: /^defaults$|^valori predefiniti$/i })
      .click();
    await expect(page).toHaveURL(/\/admin\/defaults/);
    await expect(page.getByRole("heading", { name: /runtime-managed defaults|valori predefiniti gestiti a runtime/i })).toBeVisible();

    await sections
      .getByRole("link", { name: /deployment configuration|configurazione del deployment/i })
      .click();
    await expect(page).toHaveURL(/\/admin\/deployment/);
    await expect(page.getByRole("heading", { name: /deployment configuration|configurazione del deployment/i })).toBeVisible();
  });

  test("flow 10 — viewer has no operational actions", async ({ page, context }) => {
    await applySession(context, "viewer");
    await page.goto("/");
    await expect(page.getByTestId("file-browser")).toBeVisible();
    await expect(page.getByText("note.txt").locator("visible=true").first()).toBeVisible();
    await expect(page.locator('[data-testid="more-actions-note.txt"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="desktop-actions-note.txt"]')).toHaveCount(0);
  });

  test("flow 11 — sign out", async ({ page }, testInfo) => {
    await breakGlassLogin(page);
    if (testInfo.project.name === "mobile-375") {
      await openMobileDrawer(page);
      await page
        .getByRole("dialog")
        .getByRole("button", { name: /^sign out$|^esci$/i })
        .click();
    } else {
      await page
        .getByRole("navigation", { name: /vault navigation/i })
        .getByRole("button", { name: /^sign out$|^esci$/i })
        .click();
    }
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByLabel(/username/i)).toBeVisible();
  });
});
