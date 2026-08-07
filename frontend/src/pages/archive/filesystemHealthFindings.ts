import type { FilesystemFinding } from "@/api/types";

/** Maximum finding paths rendered inline in the page-level banner. */
export const INLINE_FINDING_SAMPLE = 5;

/** Maximum group summaries rendered inline before collapsing into details. */
export const INLINE_GROUP_LIMIT = 8;

/** Page size for the full-findings dialog path list. */
export const DETAILS_PAGE_SIZE = 50;

/** Page size for the full-findings dialog group summaries. */
export const DETAILS_GROUP_PAGE_SIZE = 25;

export type FilesystemFindingGroup = {
  code: string;
  scope: string;
  /** True when at least one finding is nested under the top-level scope. */
  hasNestedPaths: boolean;
  message: string;
  remediation: string | null;
  findings: FilesystemFinding[];
  count: number;
};

/** First path segment used as a useful path scope for grouping. */
export function findingPathScope(path: string | null | undefined): string {
  const normalized = String(path || "")
    .replace(/\\/g, "/")
    .replace(/^\/+/, "");
  if (!normalized) return "";
  const slash = normalized.indexOf("/");
  // "clips" and "clips/cam1" share scope "clips"; bare files use their name.
  return slash === -1 ? normalized : normalized.slice(0, slash);
}

/**
 * Group diagnostics by finding code and top-level path scope so repeated
 * remediations (e.g. thousands of fs.unwritable_directory rows under clips/)
 * collapse into one actionable summary.
 */
export function groupFilesystemFindings(
  findings: FilesystemFinding[],
): FilesystemFindingGroup[] {
  const groups = new Map<string, FilesystemFindingGroup>();

  for (const finding of findings) {
    const scope = findingPathScope(finding.path);
    const key = `${finding.code}\0${scope}`;
    const nested = Boolean(finding.path && finding.path.includes("/"));
    const existing = groups.get(key);
    if (existing) {
      existing.findings.push(finding);
      existing.count += 1;
      existing.hasNestedPaths = existing.hasNestedPaths || nested;
      if (!existing.remediation && finding.remediation) {
        existing.remediation = finding.remediation;
      }
      if (!existing.message && finding.message) {
        existing.message = finding.message;
      }
      continue;
    }
    groups.set(key, {
      code: finding.code,
      scope,
      hasNestedPaths: nested,
      message: finding.message || finding.code,
      remediation: finding.remediation || null,
      findings: [finding],
      count: 1,
    });
  }

  return Array.from(groups.values()).sort((a, b) => {
    if (b.count !== a.count) return b.count - a.count;
    if (a.code !== b.code) return a.code.localeCompare(b.code);
    return a.scope.localeCompare(b.scope);
  });
}
