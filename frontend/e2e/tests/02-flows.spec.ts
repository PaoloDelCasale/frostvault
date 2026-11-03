import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  emergencyPasswordLogin,
  openAccountMenu,
  openMobileDrawer,
} from "../helpers/auth";

function visibleFileButton(page: Page, name: string) {
  return page
    .locator(
      `[data-testid="file-list-cards"] button[aria-label="${name}"], [data-testid="file-list-table"] button:has-text("${name}")`,
    )
    .locator("visible=true")
    .first();
}

async function openDirectory(page: Page, name: string) {
  const target = visibleFileButton(page, name);
  await expect(target).toBeVisible();
  const mobileCard = (await target.getAttribute("data-testid"))?.startsWith(
    "mobile-file-card-trigger-",
  );
  await target.click(mobileCard ? { position: { x: 20, y: 20 } } : undefined);
}

async function vaultSelect(page: Page) {
  const drawer = page.getByRole("dialog");
  if (await drawer.isVisible()) {
    return drawer.getByLabel(/^vault$/i);
  }
  return page.getByLabel(/^vault$/i).locator("visible=true").first();
}

async function chooseMenuOption(
  page: Page,
  trigger: Locator,
  name: string | RegExp,
) {
  await trigger.click();
  await page.getByRole("option", { name }).click();
}

async function languageSelect(page: Page) {
  await openAccountMenu(page);
  return page
    .getByRole("dialog", { name: /^account$/i })
    .getByLabel(/^language$|^lingua$/i);
}

test.describe("archive flows", () => {
  test("flow 2 — navigate directories, breadcrumbs, root, browser back", async ({
    page,
  }) => {
    await emergencyPasswordLogin(page);
    await openDirectory(page, "reports");
    await expect(page).toHaveURL(/directory=reports/);
    await expect(page.getByText("readme.txt").locator("visible=true").first()).toBeVisible();

    const breadcrumbs = page.getByTestId("breadcrumbs");
    await expect(breadcrumbs).toBeVisible();
    await breadcrumbs.locator('button[data-directory=""]:visible').click();
    await expect(page).not.toHaveURL(/directory=reports/);
    await expect(page.getByText("reports").locator("visible=true").first()).toBeVisible();

    await openDirectory(page, "reports");
    await page.goBack();
    await expect(page).not.toHaveURL(/directory=reports/);
  });

  test("flow 3 — search and state filter update the URL", async ({ page }) => {
    await emergencyPasswordLogin(page);
    await page.getByTestId("file-search").fill("readme");
    await expect(page).toHaveURL(/q=readme/, { timeout: 5_000 });
    await expect(page.getByText("readme.txt").locator("visible=true").first()).toBeVisible();

    await chooseMenuOption(
      page,
      page.getByTestId("state-filter"),
      /^protected$/i,
    );
    await expect(page).toHaveURL(/state=protected/);
  });

  test("flow 4 — a file opens actions without rename or path-history controls", async ({
    page,
  }) => {
    await emergencyPasswordLogin(page);
    await openDirectory(page, "reports");
    await visibleFileButton(page, "readme.txt").click();
    const actions = page.getByRole("dialog", { name: /readme.txt/ });
    await expect(actions).toBeVisible();
    // This seeded file is already local: recovery is correctly unavailable.
    await expect(actions.getByRole("button", { name: /^change storage class/i })).toHaveCount(0);
    await expect(page.getByText(/hide in cloud|confirm.*rename|path history/i)).toHaveCount(0);
  });

  test("flow 5 — cancel a destructive confirmation without side effects", async ({
    page,
  }, testInfo) => {
    await emergencyPasswordLogin(page);
    if (testInfo.project.name === "mobile-375") {
      await page
        .locator('[data-testid="more-actions-note.txt"]')
        .locator("visible=true")
        .click();
      await page
        .getByRole("button", { name: /free local space|libera spazio/i })
        .click();
    } else {
      await page.getByTestId("more-actions-desktop-note.txt").click();
      await page.getByRole("menuitem", { name: /free local space/i }).click();
    }

    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /cancel|annulla/i }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText("note.txt").locator("visible=true").first()).toBeVisible();
  });

  test("flow 6 — switch vault from the drawer/nav", async ({ page }, testInfo) => {
    await emergencyPasswordLogin(page);
    if (testInfo.project.name === "mobile-375") {
      await openMobileDrawer(page);
    }
    const select = await vaultSelect(page);
    await expect(select).toBeVisible();
    await chooseMenuOption(page, select, "Secondary Archive");
    await expect(page.getByRole("heading", { name: /secondary archive/i })).toBeVisible();
    await expect(page.getByText("hello.txt").locator("visible=true").first()).toBeVisible();
  });

  test("flow 7 — switch locale to Italian", async ({ page }) => {
    await emergencyPasswordLogin(page);
    const language = await languageSelect(page);
    await language.selectOption("it");
    await expect(page.getByTestId("file-search")).toHaveAttribute(
      "placeholder",
      /cerca per nome/i,
    );
  });

  test("flow 8 — owner reaches /vault/access without Vault quotas", async ({
    page,
  }, testInfo) => {
    await emergencyPasswordLogin(page);
    if (testInfo.project.name === "mobile-375") {
      await openMobileDrawer(page);
      await page
        .getByRole("dialog")
        .getByRole("button", { name: /vault settings|impostazioni del vault/i })
        .click();
    } else {
      await page
        .getByRole("navigation", { name: /vault navigation/i })
        .getByRole("button", { name: /vault settings|impostazioni del vault/i })
        .click();
    }
    await expect(page).toHaveURL(/\/vault\/access/);
    await expect(page.locator('[data-panel="retention"]')).toBeVisible();
    await expect(page.locator('[data-panel="quotas"]')).toHaveCount(0);
  });

  test("flow 9 — owner opens Administration and its sections", async ({
    page,
  }) => {
    await emergencyPasswordLogin(page);
    await openAccountMenu(page);
    await page
      .getByRole("dialog", { name: /^account$/i })
      .getByRole("button", { name: /administration|amministrazione/i })
      .click();
    await expect(page).toHaveURL(/\/admin/);
    await expect(page.getByText("Family Archive").first()).toBeVisible();

    const sections = page.getByRole("navigation", {
      name: /administration sections|sezioni di amministrazione/i,
    });
    await expect(
      sections.getByRole("link", { name: /users and identities|utenti e identità/i }),
    ).toHaveCount(0);

    for (const route of ["defaults", "deployment", "audit-events", "worker-errors"]) {
      await expect(sections.locator(`a[href="/admin/${route}"]`)).toHaveCount(0);
    }
    await sections.getByRole("link", { name: /source volumes/i }).click();
    await expect(page).toHaveURL(/\/admin\/sources/);
    await expect(page.getByRole("heading", { name: /source volumes/i })).toBeVisible();
    await sections.getByRole("link", { name: /metadata backups|backup dei metadati/i }).click();
    await expect(page).toHaveURL(/\/admin\/metadata-backups/);
    await expect(page.getByRole("heading", { name: /metadata backups|backup dei metadati/i }).first()).toBeVisible();
  });

  test("flow 11 — sign out", async ({ page }) => {
    await emergencyPasswordLogin(page);
    await openAccountMenu(page);
    await page
      .getByRole("dialog", { name: /^account$/i })
      .getByRole("button", { name: /^sign out$|^esci$/i })
      .click();
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByLabel(/username/i)).toBeVisible();
  });
});
