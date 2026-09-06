import assert from "node:assert/strict";
import test from "node:test";
import {
  collectFindings,
  evaluate,
  loadExceptions,
  parseArgs,
} from "./npm-audit-gate.mjs";

function advisory(id, extra = {}) {
  return {
    source: extra.source ?? 1,
    name: extra.name ?? "pkg",
    title: extra.title ?? `${id} title`,
    url: `https://github.com/advisories/${id}`,
    severity: extra.severity ?? "high",
    range: extra.range ?? "<1.0.0",
  };
}

function report(entries) {
  const vulnerabilities = {};
  for (const [name, vuln] of Object.entries(entries)) {
    vulnerabilities[name] = vuln;
  }
  return { auditReportVersion: 2, vulnerabilities };
}

const empty = report({});

const buildHigh = report({
  "brace-expansion": {
    name: "brace-expansion",
    severity: "high",
    via: [advisory("GHSA-rgw5-rvv9-x895", { name: "brace-expansion" })],
    nodes: ["node_modules/brace-expansion"],
  },
});

const runtimeModerate = report({
  qs: {
    name: "qs",
    severity: "moderate",
    via: [advisory("GHSA-4mjr-xmp4-gh2g", { name: "qs", severity: "moderate" })],
    nodes: ["node_modules/qs"],
  },
});

test("parseArgs reads required paths", () => {
  assert.deepEqual(
    parseArgs([
      "--full",
      "a.json",
      "--prod",
      "b.json",
      "--exceptions",
      "c.json",
      "--now",
      "2026-09-06",
    ]),
    { full: "a.json", prod: "b.json", exceptions: "c.json", now: "2026-09-06" },
  );
});

test("collectFindings ignores parent-package via strings", () => {
  const findings = collectFindings(
    report({
      minimatch: {
        name: "minimatch",
        severity: "high",
        via: ["brace-expansion"],
        nodes: ["node_modules/minimatch"],
      },
      "brace-expansion": {
        name: "brace-expansion",
        severity: "high",
        via: [advisory("GHSA-mh99-v99m-4gvg"), advisory("GHSA-rgw5-rvv9-x895")],
        nodes: [
          "node_modules/brace-expansion",
          "node_modules/filelist/node_modules/brace-expansion",
        ],
      },
    }),
    "full",
  );
  assert.equal(findings.length, 2);
  assert.deepEqual(
    findings.map((item) => item.id).sort(),
    ["GHSA-mh99-v99m-4gvg", "GHSA-rgw5-rvv9-x895"],
  );
});

test("clean audits pass with no exceptions", () => {
  const result = evaluate({
    full: empty,
    prod: empty,
    exceptions: [],
    now: "2026-09-06",
  });
  assert.equal(result.ok, true);
  assert.equal(result.findings.length, 0);
});

test("unexcepted build finding fails without treating it as runtime", () => {
  const result = evaluate({
    full: buildHigh,
    prod: empty,
    exceptions: [],
    now: "2026-09-06",
  });
  assert.equal(result.ok, false);
  assert.equal(result.findings[0].exposure, "build");
  assert.match(result.errors[0], /^\[build\] high GHSA-rgw5-rvv9-x895/);
});

test("prod audit classifies the same advisory as runtime", () => {
  const result = evaluate({
    full: runtimeModerate,
    prod: runtimeModerate,
    exceptions: [],
    now: "2026-09-06",
  });
  assert.equal(result.findings[0].exposure, "runtime");
  assert.match(result.errors[0], /^\[runtime\] moderate GHSA-4mjr-xmp4-gh2g/);
});

test("valid unexpired exception suppresses a build finding", () => {
  const exceptions = loadExceptions({
    exceptions: [
      {
        id: "GHSA-rgw5-rvv9-x895",
        reason: "No patched release yet; toolchain-only via minimatch.",
        issue: "https://github.com/PaoloDelCasale/frostvault/issues/308",
        exposure: "build",
        review_by: "2026-09-18",
      },
    ],
  });
  const result = evaluate({
    full: buildHigh,
    prod: empty,
    exceptions,
    now: "2026-09-06",
  });
  assert.equal(result.ok, true);
});

test("expired and unused exceptions fail", () => {
  const exceptions = loadExceptions({
    exceptions: [
      {
        id: "GHSA-rgw5-rvv9-x895",
        reason: "Waiting on upstream.",
        issue: "https://github.com/PaoloDelCasale/frostvault/issues/308",
        exposure: "build",
        review_by: "2026-09-01",
      },
      {
        id: "GHSA-2v37-7h3g-55p8",
        reason: "Already patched; leftover allowlist.",
        issue: "https://github.com/PaoloDelCasale/frostvault/issues/308",
        exposure: "build",
        review_by: "2026-09-18",
      },
    ],
  });
  const result = evaluate({
    full: buildHigh,
    prod: empty,
    exceptions,
    now: "2026-09-06",
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((line) => line.includes("expired on 2026-09-01")));
  assert.ok(result.errors.some((line) => line.includes("is unused")));
});

test("build exception cannot cover a runtime finding", () => {
  const exceptions = loadExceptions({
    exceptions: [
      {
        id: "GHSA-4mjr-xmp4-gh2g",
        reason: "Misclassified as toolchain.",
        issue: "https://github.com/PaoloDelCasale/frostvault/issues/308",
        exposure: "build",
        review_by: "2026-09-18",
      },
    ],
  });
  const result = evaluate({
    full: runtimeModerate,
    prod: runtimeModerate,
    exceptions,
    now: "2026-09-06",
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((line) => line.includes("classified build")));
});

test("exception schema rejects missing review date and issue", () => {
  assert.throws(
    () =>
      loadExceptions({
        exceptions: [
          {
            id: "GHSA-rgw5-rvv9-x895",
            reason: "No date",
            issue: "https://github.com/PaoloDelCasale/frostvault/issues/308",
            exposure: "build",
          },
        ],
      }),
    /review_by/,
  );
  assert.throws(
    () =>
      loadExceptions({
        exceptions: [
          {
            id: "not-an-id",
            reason: "bad",
            issue: "https://github.com/PaoloDelCasale/frostvault/issues/308",
            exposure: "build",
            review_by: "2026-09-18",
          },
        ],
      }),
    /GHSA or CVE/,
  );
});
