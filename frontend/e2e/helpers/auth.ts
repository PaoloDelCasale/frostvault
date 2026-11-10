import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, type BrowserContext, type Page } from "@playwright/test";

const root = path.dirname(fileURLToPath(import.meta.url));
const credentialsPath = path.join(root, "..", ".runtime", "credentials.json");
const baseURL = process.env.E2E_BASE_URL || "http://127.0.0.1:8080";

export type E2ECredentials = {
  password: string;
  owner: {
    username: string;
    password: string;
    session: string;
    csrf: string | null;
  };
  vaults: {
    primary: { id: number; name: string };
    secondary: { id: number; name: string };
  };
};

export function loadCredentials(): E2ECredentials {
  return JSON.parse(fs.readFileSync(credentialsPath, "utf8")) as E2ECredentials;
}

export async function applySession(
  context: BrowserContext,
): Promise<E2ECredentials> {
  const credentials = loadCredentials();
  const entry = credentials.owner;
  const url = baseURL.endsWith("/") ? baseURL : `${baseURL}/`;
  await context.addCookies([
    {
      name: "frostvault_session",
      value: entry.session,
      url,
      httpOnly: true,
      sameSite: "Lax",
    },
    ...(entry.csrf
      ? [
          {
            name: "frostvault_csrf",
            value: entry.csrf,
            url,
            sameSite: "Lax" as const,
          },
        ]
      : []),
  ]);
  return credentials;
}

/** Sign in as the Owner with the Emergency Password (loopback is allowed). */
export async function emergencyPasswordLogin(page: Page): Promise<E2ECredentials> {
  const credentials = loadCredentials();
  await page.goto("/login");
  await page.getByLabel(/username/i).fill(credentials.owner.username);
  await page.getByLabel(/password/i).fill(credentials.owner.password);
  await page.locator("form").getByRole("button", { name: /^sign in$|^accedi$/i }).click();
  // Sign-in lands on the Overview; the scenarios start inside the primary Vault.
  await expect(page.getByRole("heading", { level: 1, name: /^overview$|^panoramica$/i })).toBeVisible({
    timeout: 20_000,
  });
  await openVaultFiles(page, credentials.vaults.primary.id);
  await expect(page.getByRole("heading", { level: 1, name: /family archive/i })).toBeVisible();
  return credentials;
}

export const vaultFilesPath = (vaultId: number) => `/vaults/${vaultId}/files`;

/** Open a Vault's file browser by its address. */
export async function openVaultFiles(page: Page, vaultId: number): Promise<void> {
  await page.goto(vaultFilesPath(vaultId));
  await expect(page.getByTestId("file-browser")).toBeVisible();
}

/** Follow a fixed sidebar destination, through the phone menu when collapsed. */
export async function navTo(page: Page, name: string | RegExp): Promise<void> {
  await openMobileDrawer(page);
  const dialog = page.getByRole("dialog");
  const navigation = await dialog.isVisible() ? dialog.getByRole("navigation") : page.getByRole("navigation", { name: /main navigation|navigazione principale/i });
  await navigation.getByRole("link", { name }).click();
}

/** Switch the current Vault from the header switcher. */
export async function switchVault(page: Page, name: string): Promise<void> {
  await page.getByTestId("vault-switcher").click();
  await page.getByRole("dialog").getByRole("button", { name, exact: true }).click();
}

export async function openMobileDrawer(page: Page): Promise<void> {
  const trigger = page.getByRole("button", { name: /open navigation/i });
  if (await trigger.isVisible()) {
    await trigger.click();
    await expect(page.getByRole("dialog")).toBeVisible();
  }
}

/** Open the shell account / secondary-actions surface (#224). */
export async function openAccountMenu(page: Page): Promise<void> {
  const trigger = page.getByRole("button", {
    name: /open account menu|apri menu account/i,
  });
  await expect(trigger).toBeVisible();
  await trigger.click();
  await expect(
    page.getByRole("dialog", { name: /^account$/i }),
  ).toBeVisible();
}
