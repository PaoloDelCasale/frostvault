/**
 * One-off 375px screenshots for PR evidence (issue #63).
 * Run: node scripts/screenshot-auth-pages.mjs
 */
import { readFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "node:http";
import { chromium } from "playwright";

const root = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(root, "..");
const dist = path.join(frontendRoot, "dist");
const localesDir = path.resolve(frontendRoot, "../app/locales");
const artifacts = path.join(frontendRoot, "artifacts");

const en = JSON.parse(readFileSync(path.join(localesDir, "en.json"), "utf8"));

function contentType(filePath) {
  if (filePath.endsWith(".html")) return "text/html; charset=utf-8";
  if (filePath.endsWith(".js")) return "application/javascript";
  if (filePath.endsWith(".css")) return "text/css";
  return "application/octet-stream";
}

const server = createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://127.0.0.1");
  if (url.pathname.startsWith("/api/i18n/catalog")) {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({ locale: "en", locales: ["en", "it"], messages: en }),
    );
    return;
  }
  let filePath = path.join(dist, url.pathname === "/" ? "index.html" : url.pathname);
  if (
    url.pathname === "/login" ||
    url.pathname === "/no-vault" ||
    !url.pathname.includes(".")
  ) {
    filePath = path.join(dist, "index.html");
  }
  try {
    const body = readFileSync(filePath);
    res.writeHead(200, { "Content-Type": contentType(filePath) });
    res.end(body);
  } catch {
    res.writeHead(404);
    res.end("not found");
  }
});

await new Promise((resolve) => server.listen(4177, "127.0.0.1", resolve));
mkdirSync(artifacts, { recursive: true });

const browser = await chromium.launch({
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const page = await browser.newPage({ viewport: { width: 375, height: 812 } });

await page.goto("http://127.0.0.1:4177/login", { waitUntil: "networkidle" });
await page.waitForSelector("text=Welcome");
await page.screenshot({
  path: path.join(artifacts, "login-375px.png"),
  fullPage: true,
});

await page.goto("http://127.0.0.1:4177/no-vault", { waitUntil: "networkidle" });
await page.waitForSelector("text=No vault assigned yet");
await page.screenshot({
  path: path.join(artifacts, "no-vault-375px.png"),
  fullPage: true,
});

await browser.close();
server.close();
console.log("Wrote login-375px.png and no-vault-375px.png");
