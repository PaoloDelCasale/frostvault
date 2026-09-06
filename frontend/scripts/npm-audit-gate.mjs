#!/usr/bin/env node
/**
 * Fail CI when npm audit reports unexcepted vulnerabilities.
 *
 * Distinguishes runtime (present in `npm audit --omit=dev`) from build/toolchain
 * exposure (dev-only, including transitive copies). Exceptions must name a GHSA
 * or CVE, a reason, a GitHub issue URL, an exposure class, and a review date.
 */
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const SEVERITY_RANK = {
  critical: 4,
  high: 3,
  moderate: 2,
  low: 1,
  info: 0,
};

const EXPOSURES = new Set(["runtime", "build"]);
const ID_PATTERN = /^(GHSA-[0-9a-z-]+|CVE-\d{4}-\d+)$/i;
const ISSUE_PATTERN = /^https:\/\/github\.com\/[^/]+\/[^/]+\/issues\/\d+$/;
const DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

export function parseArgs(argv) {
  const options = { full: "", prod: "", exceptions: "", now: "" };
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (key === "--full" && value) options.full = value;
    else if (key === "--prod" && value) options.prod = value;
    else if (key === "--exceptions" && value) options.exceptions = value;
    else if (key === "--now" && value) options.now = value;
    else continue;
    index += 1;
  }
  return options;
}

export function loadJson(filePath, label) {
  let raw;
  try {
    raw = fs.readFileSync(filePath, "utf8");
  } catch (error) {
    throw new Error(`${label} not readable: ${filePath}: ${error.message}`);
  }
  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new Error(`${label} is not valid JSON: ${filePath}: ${error.message}`);
  }
}

function ghsaFromUrl(url) {
  if (typeof url !== "string" || !url) return "";
  const ghsa = url.match(/GHSA-[0-9a-z-]+/i);
  if (ghsa) return canonicalizeId(ghsa[0]);
  const cve = url.match(/CVE-\d{4}-\d+/i);
  return cve ? canonicalizeId(cve[0]) : "";
}

function canonicalizeId(value) {
  const text = String(value || "").trim();
  const ghsa = text.match(/^GHSA-([0-9a-z-]+)$/i);
  if (ghsa) return `GHSA-${ghsa[1].toLowerCase()}`;
  const cve = text.match(/^CVE-(\d{4})-(\d+)$/i);
  if (cve) return `CVE-${cve[1]}-${cve[2]}`;
  return text.toUpperCase();
}

function normalizeId(value) {
  return canonicalizeId(value);
}

function worseSeverity(left, right) {
  const leftRank = SEVERITY_RANK[left] ?? -1;
  const rightRank = SEVERITY_RANK[right] ?? -1;
  return rightRank > leftRank ? right : left;
}

function utcDateString(value) {
  if (value instanceof Date) {
    return value.toISOString().slice(0, 10);
  }
  const text = String(value || "").trim();
  if (DATE_PATTERN.test(text)) return text;
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) {
    throw new Error(`invalid date: ${value}`);
  }
  return parsed.toISOString().slice(0, 10);
}

function parseReviewDate(text, label) {
  const match = DATE_PATTERN.exec(String(text || "").trim());
  if (!match) {
    throw new Error(`${label} must be YYYY-MM-DD`);
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  ) {
    throw new Error(`${label} is not a real calendar date: ${text}`);
  }
  return `${match[1]}-${match[2]}-${match[3]}`;
}

export function collectFindings(audit, label) {
  if (!audit || typeof audit !== "object") {
    throw new Error(`${label} audit report is missing`);
  }
  if (audit.error) {
    const summary = audit.error.summary || audit.error.message || JSON.stringify(audit.error);
    throw new Error(`${label} npm audit failed: ${summary}`);
  }
  const vulnerabilities = audit.vulnerabilities;
  if (!vulnerabilities || typeof vulnerabilities !== "object") {
    throw new Error(`${label} audit report has no vulnerabilities object`);
  }
  const byId = new Map();
  for (const [pkg, vuln] of Object.entries(vulnerabilities)) {
    if (!vuln || typeof vuln !== "object") continue;
    for (const via of vuln.via || []) {
      if (!via || typeof via !== "object") continue;
      const url = via.url || "";
      const id = normalizeId(ghsaFromUrl(url) || via.source);
      if (!id) continue;
      const current = byId.get(id) || {
        id,
        packages: new Set(),
        severity: "info",
        title: "",
        url,
        nodes: new Set(),
      };
      current.packages.add(pkg);
      current.severity = worseSeverity(
        current.severity,
        via.severity || vuln.severity || "info",
      );
      current.title = current.title || via.title || "";
      current.url = current.url || url;
      for (const node of vuln.nodes || []) current.nodes.add(node);
      byId.set(id, current);
    }
  }
  return [...byId.values()].map((finding) => ({
    id: finding.id,
    packages: [...finding.packages].sort(),
    severity: finding.severity,
    title: finding.title,
    url: finding.url,
    nodes: [...finding.nodes].sort(),
  }));
}

export function loadExceptions(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("exceptions file must be an object with an exceptions array");
  }
  if (!Array.isArray(payload.exceptions)) {
    throw new Error("exceptions file must contain an exceptions array");
  }
  const seen = new Set();
  return payload.exceptions.map((entry, index) => {
    const label = `exceptions[${index}]`;
    if (!entry || typeof entry !== "object") {
      throw new Error(`${label} must be an object`);
    }
    const id = normalizeId(entry.id);
    if (!ID_PATTERN.test(id)) {
      throw new Error(`${label}.id must be a GHSA or CVE identifier`);
    }
    if (seen.has(id)) {
      throw new Error(`${label}.id duplicates ${id}`);
    }
    seen.add(id);
    const reason = String(entry.reason || "").trim();
    if (!reason) {
      throw new Error(`${label}.reason is required`);
    }
    const issue = String(entry.issue || "").trim();
    if (!ISSUE_PATTERN.test(issue)) {
      throw new Error(`${label}.issue must be a GitHub issue URL`);
    }
    const exposure = String(entry.exposure || "").trim();
    if (!EXPOSURES.has(exposure)) {
      throw new Error(`${label}.exposure must be "runtime" or "build"`);
    }
    return {
      id,
      reason,
      issue,
      exposure,
      review_by: parseReviewDate(entry.review_by, `${label}.review_by`),
    };
  });
}

export function evaluate({ full, prod, exceptions, now }) {
  const lines = [];
  const errors = [];
  const today = utcDateString(now || new Date());
  const fullFindings = collectFindings(full, "full");
  const prodIds = new Set(collectFindings(prod, "prod").map((item) => item.id));
  const allow = new Map(exceptions.map((item) => [item.id, item]));

  const classified = fullFindings.map((finding) => ({
    ...finding,
    exposure: prodIds.has(finding.id) ? "runtime" : "build",
  }));

  for (const finding of classified) {
    const exception = allow.get(finding.id);
    if (!exception) {
      errors.push(
        `[${finding.exposure}] ${finding.severity} ${finding.id} ${finding.packages.join(", ")}: ${finding.title}\n    nodes: ${finding.nodes.join(", ") || "(none)"}\n    exception: none`,
      );
      continue;
    }
    if (exception.review_by < today) {
      errors.push(
        `exception ${finding.id} expired on ${exception.review_by} (${exception.issue})`,
      );
    }
    if (exception.exposure === "build" && finding.exposure === "runtime") {
      errors.push(
        `exception ${finding.id} is classified build but the advisory is in the runtime tree`,
      );
    }
  }

  for (const exception of exceptions) {
    if (!classified.some((finding) => finding.id === exception.id)) {
      errors.push(
        `exception ${exception.id} is unused (${exception.issue}); remove it`,
      );
    }
  }

  const runtimeCount = classified.filter((item) => item.exposure === "runtime").length;
  const buildCount = classified.filter((item) => item.exposure === "build").length;
  lines.push("npm audit gate");
  lines.push(`  runtime findings: ${runtimeCount}`);
  lines.push(`  build findings: ${buildCount}`);
  lines.push(`  exceptions: ${exceptions.length}`);
  if (errors.length) {
    lines.push("  status: fail");
    lines.push("npm audit gate failed:");
    for (const error of errors) {
      const [first, ...rest] = error.split("\n");
      lines.push(`  - ${first}`);
      for (const extra of rest) lines.push(extra);
    }
  } else {
    lines.push("  status: pass");
  }
  return { ok: errors.length === 0, lines, findings: classified, errors };
}

export function main(argv) {
  const options = parseArgs(argv);
  if (!options.full || !options.prod || !options.exceptions) {
    console.error(
      "usage: node scripts/npm-audit-gate.mjs --full <audit.json> --prod <audit-prod.json> --exceptions <exceptions.json> [--now YYYY-MM-DD]",
    );
    return 2;
  }
  try {
    const result = evaluate({
      full: loadJson(options.full, "full audit"),
      prod: loadJson(options.prod, "prod audit"),
      exceptions: loadExceptions(loadJson(options.exceptions, "exceptions")),
      now: options.now || new Date(),
    });
    console.log(result.lines.join("\n"));
    return result.ok ? 0 : 1;
  } catch (error) {
    console.error(`npm audit gate error: ${error.message}`);
    return 2;
  }
}

function isExecutedDirectly() {
  const invoked = process.argv[1];
  if (!invoked) return false;
  try {
    return pathToFileURL(fs.realpathSync(path.resolve(invoked))).href === import.meta.url;
  } catch {
    return false;
  }
}

if (isExecutedDirectly()) {
  process.exit(main(process.argv.slice(2)));
}
