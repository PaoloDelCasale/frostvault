#!/usr/bin/env node
/**
 * Capture 375px evidence screenshots for the responsive file browser.
 * Build the capture bundle first: VITE_ALLOW_DEMO=1 npm run build
 * Then run: node scripts/capture-file-browser-screenshots.mjs
 * Requires a Vite preview (or dev) server and google-chrome.
 */
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const artifacts = path.join(root, "artifacts");
const port = 5179;
const base = `http://127.0.0.1:${port}`;

async function waitForServer(url, attempts = 60) {
  for (let i = 0; i < attempts; i++) {
    try {
      const res = await fetch(url);
      if (res.ok || res.status === 404) return;
    } catch {
      // retry
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`Server did not start: ${url}`);
}

function runChrome(args) {
  return new Promise((resolve, reject) => {
    const child = spawn("google-chrome", args, {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("exit", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`chrome exited ${code}: ${stderr}`));
    });
  });
}

async function screenshot(url, outfile) {
  const profile = await mkdtemp(path.join(tmpdir(), "fv-chrome-"));
  try {
    await runChrome([
      "--headless=new",
      "--disable-gpu",
      "--no-sandbox",
      "--hide-scrollbars",
      `--user-data-dir=${profile}`,
      `--window-size=375,812`,
      `--screenshot=${outfile}`,
      url,
    ]);
  } finally {
    await rm(profile, { recursive: true, force: true });
  }
}

async function main() {
  await mkdir(artifacts, { recursive: true });

  const preview = spawn(
    "npx",
    ["vite", "preview", "--host", "127.0.0.1", "--port", String(port)],
    { cwd: root, stdio: ["ignore", "pipe", "pipe"] },
  );

  try {
    await waitForServer(`${base}/?demo=files`);

    // Root listing
    await screenshot(
      `${base}/?demo=files`,
      path.join(artifacts, "file-browser-root-375px.png"),
    );

    // Nested directory
    await screenshot(
      `${base}/?demo=files&directory=reports`,
      path.join(artifacts, "file-browser-nested-375px.png"),
    );

    // Active search
    await screenshot(
      `${base}/?demo=files&q=lease`,
      path.join(artifacts, "file-browser-search-375px.png"),
    );

    // Path History — open via a small HTML helper that loads the app then clicks
    const historyHtml = path.join(artifacts, "_history-helper.html");
    // Use chrome remote debugging to click is heavy; instead navigate with
    // a data URL is not possible for SPA. Use a puppeteer-less approach:
    // evaluate via chrome --virtual-time-budget after injecting click through
    // a dedicated query flag handled in demo.
    await screenshot(
      `${base}/?demo=files&history=readme.txt`,
      path.join(artifacts, "file-browser-path-history-375px.png"),
    );

    console.log("Wrote 375px screenshots to", artifacts);
    void historyHtml;
    void writeFile;
    void readFile;
  } finally {
    preview.kill("SIGTERM");
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
