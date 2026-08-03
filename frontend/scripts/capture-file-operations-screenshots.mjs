#!/usr/bin/env node
/**
 * Capture 375px evidence screenshots for file operations (issue #67).
 * Build the capture bundle first: VITE_ALLOW_DEMO=1 npm run build
 * Then run: node scripts/capture-file-operations-screenshots.mjs
 * Requires a Vite preview server and google-chrome.
 */
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const artifacts = path.join(root, "artifacts");
const port = 5180;
const base = `http://127.0.0.1:${port}`;

async function waitForServer(url, attempts = 80) {
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
      "--virtual-time-budget=8000",
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
      `${base}/?demo=files&sheet=archive.pdf`,
      path.join(artifacts, "file-ops-bottom-sheet-375px.png"),
    );
    await screenshot(
      `${base}/?demo=files&versions=archive.pdf`,
      path.join(artifacts, "file-ops-version-dialog-375px.png"),
    );
    await screenshot(
      `${base}/?demo=files&confirm=free-space&target=readme.txt`,
      path.join(artifacts, "file-ops-confirm-375px.png"),
    );
    await screenshot(
      `${base}/?demo=files&job=1`,
      path.join(artifacts, "file-ops-job-progress-375px.png"),
    );

    console.log("Wrote 375px file-ops screenshots to", artifacts);
  } finally {
    preview.kill("SIGTERM");
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
