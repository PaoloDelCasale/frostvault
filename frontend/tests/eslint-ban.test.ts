import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const fixturePath = path.join(
  frontendRoot,
  "eslint-fixtures",
  "with-dangerously-set-inner-html.tsx",
);

describe("ESLint ban on dangerouslySetInnerHTML", () => {
  it("fails linting a fixture that uses dangerouslySetInnerHTML", () => {
    const result = spawnSync(
      path.join(frontendRoot, "node_modules", ".bin", "eslint"),
      ["--no-ignore", fixturePath],
      {
        cwd: frontendRoot,
        encoding: "utf8",
        env: process.env,
      },
    );

    expect(result.status).not.toBe(0);
    expect(`${result.stdout}\n${result.stderr}`).toMatch(/dangerouslySetInnerHTML|react\/no-danger/i);
  });
});
