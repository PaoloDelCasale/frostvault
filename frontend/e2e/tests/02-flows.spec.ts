import { expect, test, type Page } from "@playwright/test";
import { emergencyPasswordLogin, navTo, openAccountMenu, switchVault, vaultFilesPath } from "../helpers/auth";
const visible = (page: Page, name: string | RegExp) => page.getByRole("button", { name }).locator("visible=true").first();

test.describe("essential Owner flows (#365 D1–D17)", () => {
  test("folder navigation, breadcrumbs and browser Back", async ({ page }) => {
    await emergencyPasswordLogin(page);
    await visible(page, /^reports$/).click();
    await expect(page).toHaveURL(/directory=reports/);
    await expect(visible(page, "readme.txt")).toBeVisible();
    await page.getByTestId("breadcrumbs").locator('button[data-directory=""]').click();
    await expect(page).not.toHaveURL(/directory=reports/);
    await page.goBack(); await expect(page).toHaveURL(/directory=reports/);
  });

  test("the header switcher changes Vault and the sidebar remembers it at its root", async ({ page }) => {
    const credentials = await emergencyPasswordLogin(page);
    await switchVault(page, "Secondary Archive");
    await expect(page).toHaveURL(new RegExp(`${vaultFilesPath(credentials.vaults.secondary.id)}$`));
    await expect(page.getByRole("heading", { name: "Secondary Archive", exact: true })).toBeVisible();
    await visible(page, /^photos$/).click();
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: /^overview$/i })).toBeVisible();
    await navTo(page, /^files$/i);
    await expect(page.getByRole("heading", { name: "Secondary Archive", exact: true })).toBeVisible();
    await expect(visible(page, "hello.txt")).toBeVisible();
    await page.goto(vaultFilesPath(credentials.vaults.primary.id));
  });

  test("global Cloud-only name/path search retains duplicate paths and the correct Vault during recovery", async ({ page }) => {
    const credentials = await emergencyPasswordLogin(page);
    await page.getByTestId("file-search").fill("photos/cold");
    await page.getByTestId("search-scope").getByRole("button", { name: /^all vaults$/i }).click();
    await page.getByTestId("state-filter").getByRole("button", { name: /^cloud only/i }).click();
    await expect(visible(page, "Family Archive / photos/cold.jpg")).toBeVisible();
    await expect(visible(page, "Secondary Archive / photos/cold.jpg")).toBeVisible();
    await visible(page, "Secondary Archive / photos/cold.jpg").click();
    await expect(page).toHaveURL(new RegExp(`${vaultFilesPath(credentials.vaults.secondary.id)}\\?`));
    await expect(page.locator('[data-highlighted="true"]:visible')).toHaveCount(1);
    const details = page.getByRole("dialog", { name: "cold.jpg" });
    await expect(details).toBeVisible();
    await details.getByRole("button", { name: /^recover$/i }).click();
    const recovery = page.getByRole("dialog", { name: /^recover$/i });
    await expect(recovery.getByRole("combobox")).toHaveValue("Bulk");
    await expect(recovery).toContainText(/returns to: secondary archive/i);
    await expect(recovery).toContainText(/automatic release is off/i);
    await recovery.getByRole("combobox").selectOption("Standard");
    await expect(recovery.getByRole("combobox")).toHaveValue("Standard");
    const request = page.waitForRequest((req) => req.method() === "POST" && new URL(req.url()).pathname === "/api/recover");
    await recovery.getByRole("button", { name: /^recover$/i }).click();
    const sent = await request;
    expect(new URL(sent.url()).searchParams.get("vault_id")).toBe(String(credentials.vaults.secondary.id));
    expect(sent.postDataJSON()).toMatchObject({ path: "photos/cold.jpg", restore_tier: "Standard" });
    await expect(recovery).toBeHidden();
  });

  test("folder recovery estimates cold descendants with Bulk default before deliberate confirmation", async ({ page }) => {
    await emergencyPasswordLogin(page);
    await page.getByRole("checkbox", { name: /select family archive \/ photos$/i }).locator("visible=true").check();
    await page.getByRole("button", { name: /^recover$/i }).click();
    const recovery = page.getByRole("dialog", { name: /^recover$/i });
    await expect(recovery.getByRole("combobox")).toHaveValue("Bulk");
    await expect(recovery).toContainText(/returns to: family archive \/ photos/i);
    await expect(recovery).toContainText(/estimated.*EUR/i);
    await recovery.getByRole("button", { name: /^cancel$/i }).click();
    await expect(recovery).toBeHidden();
  });

  test("problem link opens the folder, highlights the row and offers an explicit remedy", async ({ page }) => {
    const credentials = await emergencyPasswordLogin(page);
    await navTo(page, /^problems: \d+ open$/i);
    const problem = page.locator("article").filter({ hasText: "Conflict with a file kept only in the cloud" });
    await problem.getByRole("link", { name: /open problem and remedy/i }).click();
    await expect(page).toHaveURL(new RegExp(`${vaultFilesPath(credentials.vaults.primary.id)}\\?`));
    const details = page.getByRole("dialog", { name: "conflict.jpg" });
    await expect(details).toContainText(/rename the new file outside frostvault/i);
    await expect(page.locator('[data-highlighted="true"]:visible')).toHaveCount(1);
    await details.getByRole("button", { name: /replace the cloud copy/i }).click();
    const confirm = page.getByRole("alertdialog");
    await expect(confirm).toContainText(/only recoverable cloud copy/i);
    const response = page.waitForResponse((res) => res.request().method() === "POST" && new URL(res.url()).pathname === "/api/files/cloud-only-conflict/replace");
    await confirm.getByRole("button", { name: /replace the cloud copy/i }).click();
    expect((await response).status()).toBe(202);
  });

  test("release is deliberate; Vault settings use fixed periods and a collapsed storage plan", async ({ page }) => {
    await emergencyPasswordLogin(page);
    await page.getByRole("checkbox", { name: /select family archive \/ note.txt/i }).locator("visible=true").check();
    await page.getByRole("button", { name: /free space now/i }).click();
    const confirm = page.getByRole("alertdialog"); await expect(confirm).toBeVisible();
    await confirm.getByRole("button", { name: /cancel/i }).click();
    await navTo(page, /^settings$/i);
    await expect(page).toHaveURL(/\/vaults\/\d+\/settings$/);
    const release = page.locator('[data-panel="retention"]');
    await release.getByRole("checkbox").check();
    await release.getByRole("combobox").selectOption("180");
    await release.getByRole("button", { name: /save local copy retention/i }).click();
    await expect(page.getByRole("status").filter({ hasText: /enabled/i })).toBeVisible();
    await expect(page.locator('[data-panel="lifecycle"]')).toBeHidden();
    // Leave the shared fixture's automatic release off for the other scenarios.
    await release.getByRole("checkbox").uncheck();
    await release.getByRole("button", { name: /save local copy retention/i }).click();
  });

  test("System exposes email warning, kit fingerprint/custody and Vault creation", async ({ page }) => {
    await emergencyPasswordLogin(page); await navTo(page, /^system$/i);
    await expect(page.getByTestId("email-not-configured")).toBeVisible();
    await expect(page.getByText(/last verified metadata backup/i)).toBeVisible();
    await expect(page.getByText(/^current kit fingerprint/i)).toBeVisible();
    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: /download.*recovery kit/i }).click();
    expect((await download).suggestedFilename()).toContain("recovery-kit");
    await page.getByRole("button", { name: /i have stored|custod|saved.*outside/i }).click();
    await expect(page.getByRole("status").filter({ hasText: /custody|stored|acknowledged/i })).toBeVisible();
    await page.getByRole("link", { name: /protect a folder/i }).click();
    await page.getByLabel(/^vault name/i).fill(`ZZ Essential ${Date.now()}`);
    await page.locator("form").getByRole("button", { name: /create vault/i }).click();
    await expect(page.getByTestId("file-browser")).toBeVisible();
    await expect(page.getByRole("heading", { name: /^ZZ Essential/ })).toBeVisible();
  });

  test("language and sign out remain in Owner preferences", async ({ page }) => {
    await emergencyPasswordLogin(page); await openAccountMenu(page);
    const account = page.getByRole("dialog", { name: /^account$/i });
    await account.getByRole("combobox", { name: /^language$/i }).selectOption("it");
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("file-search")).toHaveAttribute("placeholder", /^cerca in questo vault/i);
    await openAccountMenu(page); await page.getByRole("button", { name: /^esci$/i }).click();
    await expect(page).toHaveURL(/\/login/);
  });
});
