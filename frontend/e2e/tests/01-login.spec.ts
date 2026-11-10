import { expect, test } from "@playwright/test";

import { emergencyPasswordLogin, vaultFilesPath } from "../helpers/auth";

test.describe("flow 1 — Break-glass Login", () => {
  test("Break-glass Login opens the Overview, then the archive", async ({ page }, testInfo) => {
    const credentials = await emergencyPasswordLogin(page);
    await page.goto("/");
    const vaults = page.getByTestId(testInfo.project.name === "mobile-375" ? "overview-cards" : "overview-table");
    await expect(vaults.getByRole("link", { name: credentials.vaults.secondary.name })).toBeVisible();
    await vaults.getByRole("link", { name: credentials.vaults.primary.name }).click();
    await expect(page).toHaveURL(new RegExp(`${vaultFilesPath(credentials.vaults.primary.id)}$`));
    await expect(page.getByTestId("file-browser")).toBeVisible();
    await expect(page.getByText("reports").locator("visible=true").first()).toBeVisible();
    await expect(page.getByText("note.txt").locator("visible=true").first()).toBeVisible();

    if (testInfo.project.name === "mobile-375") {
      await page.screenshot({
        path: "artifacts/e2e-login-archive-375px.png",
        fullPage: true,
      });
    }
  });
});
