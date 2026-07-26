import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, type BrowserContext, type Page } from "@playwright/test";

const root = path.dirname(fileURLToPath(import.meta.url));
const credentialsPath = path.join(root, "..", ".runtime", "credentials.json");
const baseURL = process.env.E2E_BASE_URL || "http://127.0.0.1:8080";

export type E2ECredentials = {
  password: string;
  admin: {
    username: string;
    password: string;
    session: string;
    csrf: string | null;
  };
  operator: { username: string; session: string; csrf: string | null };
  viewer: { username: string; session: string; csrf: string | null };
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
  role: "admin" | "operator" | "viewer",
): Promise<E2ECredentials> {
  const credentials = loadCredentials();
  const entry = credentials[role];
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

export async function breakGlassLogin(page: Page): Promise<E2ECredentials> {
  const credentials = loadCredentials();
  await page.goto("/login");
  await page.getByLabel(/username/i).fill(credentials.admin.username);
  await page.getByLabel(/password/i).fill(credentials.admin.password);
  await page.locator("form").getByRole("button", { name: /^sign in$|^accedi$/i }).click();
  await expect(page.getByRole("heading", { name: /family archive/i })).toBeVisible({
    timeout: 20_000,
  });
  return credentials;
}

export async function openMobileDrawer(page: Page): Promise<void> {
  const trigger = page.getByRole("button", { name: /open navigation/i });
  if (await trigger.isVisible()) {
    await trigger.click();
    await expect(page.getByRole("dialog")).toBeVisible();
  }
}
