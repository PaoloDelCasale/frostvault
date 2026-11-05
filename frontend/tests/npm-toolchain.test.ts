// @vitest-environment node
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { clearCaches, parseAndGenerateServices } from "@typescript-eslint/typescript-estree";
import { afterEach, describe, expect, it } from "vitest";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const estreeRequire = createRequire(require.resolve("@typescript-eslint/typescript-estree"));
const read = (file: string) => readFileSync(path.join(frontend, file), "utf8");

afterEach(() => clearCaches());

describe("scoped minimatch 10 → 11 compatibility (#405)", () => {
  it("resolves the patched named CommonJS API from the actual estree consumer", () => {
    const { version, engines } = estreeRequire("minimatch/package.json");
    const semver = estreeRequire("semver");
    expect(semver.satisfies(version, "^11.0.1")).toBe(true);
    for (const nodeVersion of ["20.19.0", "22.12.0", "24.0.0"]) {
      expect(semver.satisfies(nodeVersion, engines.node)).toBe(true);
    }
    const { minimatch } = estreeRequire("minimatch");
    expect(minimatch(".config.cjs", "*.cjs", { dot: true })).toBe(true);
    expect(minimatch("nested/config.cjs", "*.cjs", { dot: true })).toBe(false);
  });

  it("leaves the safe callable minimatch 3/5 APIs used by ESLint and Redocly alone", () => {
    for (const [consumer, major] of [["eslint", 3], ["eslint-plugin-react", 3], ["@redocly/openapi-core", 5]] as const) {
      const consumerRequire = createRequire(require.resolve(consumer));
      expect(consumerRequire("minimatch/package.json").version).toMatch(new RegExp(`^${major}\\.`));
      const minimatch = consumerRequire("minimatch");
      expect(typeof minimatch).toBe("function");
      expect(minimatch("src/example.ts", "src/*.ts")).toBe(true);
      expect(minimatch("src/nested/example.ts", "src/*.ts")).toBe(false);
    }
  });

  // Public parser + real TypeScript project service exercise the private
  // filePathMatchedBy() path, not a reimplementation of its minimatch call.
  // Source text is supplied in memory; no fixture files or tsconfigs are written.
  it.each([
    [".audit-compat.cjs", "*.cjs", true], // estree explicitly enables dot:true
    ["audit-compat.cjs", "*.{cjs,mjs}", true],
    ["fixtures/.audit-compat.cjs", "fixtures/*.cjs", true],
    ["audit-compat.cjs", "*.mjs", false],
    ["fixtures/audit-compat.cjs", "*.cjs", false],
    ["fixtures/nested/audit-compat.cjs", "fixtures/*.cjs", false],
  ])("allowDefaultProject matches %s with %s: %s", (file, pattern, matches) => {
    const parse = () => parseAndGenerateServices("const example = 1;", {
      filePath: path.join(frontend, file),
      tsconfigRootDir: frontend,
      projectService: { allowDefaultProject: [pattern] },
      loggerFn: false,
    });
    if (matches) {
      expect(parse().services.program).not.toBeNull();
    } else {
      expect(parse).toThrow(/was not found by the project service/);
    }
  });
});

describe("vendored shadcn CSS retains the design system without the CLI", () => {
  const vendor = "src/vendor/shadcn";
  const cssHash = "4c371f7a1ff5d219ae2f7ff28bd256b4346fd546fe46fbae22092e57db2f0fae";
  const licenseHash = "1564074e13439397221ffd522e2e504d56561994a23d371aa5e3ad43e4f5423f";

  it("retains every upstream CSS byte and the exact MIT license/attribution", () => {
    const css = readFileSync(path.join(frontend, vendor, "tailwind.css"));
    expect(css.byteLength).toBe(16340);
    expect(createHash("sha256").update(css).digest("hex")).toBe(cssHash);
    const license = read(`${vendor}/LICENSE.md`);
    expect(createHash("sha256").update(license).digest("hex")).toBe(licenseHash);
    expect(license).toContain("Copyright (c) 2023 shadcn");
    const provenance = read(`${vendor}/README.md`);
    expect(provenance).toContain("shadcn@4.22.1/dist/tailwind.css");
    expect(provenance).toContain("daaf156ee00ed91cc7e6bdd937c1b4973152d141");
    expect(provenance).toContain(cssHash);
    expect(provenance).toContain(licenseHash);
  });

  it("keeps the stylesheet on the existing Vite/Tailwind application import path", () => {
    const css = read("src/index.css");
    expect(css).toContain('@import "./vendor/shadcn/tailwind.css";');
    expect(css).not.toContain('@import "shadcn/tailwind.css"');
    expect(css.indexOf('@import "tailwindcss";')).toBeLessThan(css.indexOf('@import "./vendor/shadcn/tailwind.css";'));
    expect(read("src/main.tsx")).toContain('import "./index.css"');
    expect(read("vite.config.ts")).toContain("tailwindcss()");
  });
});
