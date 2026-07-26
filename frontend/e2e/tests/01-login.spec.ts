import { expect, test } from "@playwright/test";

import { breakGlassLogin } from "../helpers/auth";

test.describe("flow 1 — Break-glass Login", () => {
  test("Break-glass Login shows the archive", async ({ page }, testInfo) => {
    await breakGlassLogin(page);
    await expect(page.getByTestId("file-browser")).toBeVisible();
    await expect(page.getByText("reports").first()).toBeVisible();
    await expect(page.getByText("note.txt").first()).toBeVisible();

    if (testInfo.project.name === "mobile-375") {
      await page.screenshot({
        path: "artifacts/e2e-login-archive-375px.png",
        fullPage: true,
      });
    }
  });
});
