#!/usr/bin/env node
/**
 * Capture 375px evidence screenshots for storage-class + pin (issue #110).
 * Requires Vite preview and google-chrome/chromium.
 */
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, copyFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const artifacts = path.join(root, "artifacts");
const outDir = "/opt/cursor/artifacts/screenshots";
const port = 5181;
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

function chromeBin() {
  return process.env.CHROME_BIN || "google-chrome";
}

function runChrome(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(chromeBin(), args, {
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
      "--virtual-time-budget=10000",
      `--screenshot=${outfile}`,
      url,
    ]);
  } finally {
    await rm(profile, { recursive: true, force: true });
  }
}

async function main() {
  await mkdir(artifacts, { recursive: true });
  await mkdir(outDir, { recursive: true });

  const preview = spawn(
    "npx",
    ["vite", "preview", "--host", "127.0.0.1", "--port", String(port)],
    { cwd: root, stdio: ["ignore", "pipe", "pipe"] },
  );

  const shots = [
    ["storage-class-picker-375px.png", `${base}/?demo=files&confirm=storage-class&target=archive.pdf`],
    ["storage-class-confirm-warning-375px.png", `${base}/?demo=files&confirm=storage-class&target=readme.txt`],
    ["lifecycle-pin-confirm-375px.png", `${base}/?demo=files&confirm=lifecycle-pin&target=archive.pdf`],
    ["lifecycle-pinned-row-375px.png", `${base}/?demo=files`],
    ["storage-class-job-progress-375px.png", `${base}/?demo=files&job=storage-class`],
  ];

  try {
    await waitForServer(`${base}/?demo=files`);
    for (const [name, url] of shots) {
      const local = path.join(artifacts, name);
      await screenshot(url, local);
      await copyFile(local, path.join(outDir, name));
      console.log("wrote", name);
    }
  } finally {
    preview.kill("SIGTERM");
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
