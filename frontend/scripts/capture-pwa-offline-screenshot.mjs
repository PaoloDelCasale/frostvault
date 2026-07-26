#!/usr/bin/env node
/**
 * Capture 375px evidence for PWA offline stale listing (issue #72).
 */
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const artifacts = path.join(root, "artifacts");
const port = 5181;
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
      "--window-size=375,812",
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
    await screenshot(
      `${base}/?demo=files&offline=1`,
      path.join(artifacts, "pwa-offline-stale-375px.png"),
    );
    console.log("Wrote", path.join(artifacts, "pwa-offline-stale-375px.png"));
  } finally {
    preview.kill("SIGTERM");
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
