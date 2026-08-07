import { useMemo, useState } from "react";

import type { FilesystemFinding, FilesystemHealth } from "@/api/types";
import { Dialog } from "@/components/Dialog";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import {
  DETAILS_GROUP_PAGE_SIZE,
  DETAILS_PAGE_SIZE,
  INLINE_FINDING_SAMPLE,
  INLINE_GROUP_LIMIT,
  type FilesystemFindingGroup,
  groupFilesystemFindings,
  resolveFindingsTotal,
  resolveHealthStatus,
} from "./filesystemHealthFindings";

type Translate = (key: string, params?: Record<string, string | number>) => string;

type FilesystemHealthBannerProps = {
  filesystem: FilesystemHealth | null | undefined;
  t: Translate;
  className?: string;
};

function countedLabel(
  count: number,
  baseKey: string,
  t: Translate,
  params: Record<string, string | number> = {},
): string {
  const form = count === 1 ? "one" : "other";
  return t(`${baseKey}_${form}`, { count, ...params });
}

/** Finding and group counts pluralize independently (e.g. "2 findings in 1 group"). */
function findingsSummaryLabel(
  findingCount: number,
  groupCount: number,
  t: Translate,
): string {
  return t("ui.filesystem_findings_summary", {
    findings: countedLabel(findingCount, "ui.filesystem_findings_count", t),
    groups: countedLabel(groupCount, "ui.filesystem_groups_count", t),
  });
}

function groupSummaryLabel(group: FilesystemFindingGroup, t: Translate): string {
  const params = {
    message: group.message,
    code: group.code,
    count: group.count,
    scope: group.scope,
  };
  if (group.scope && group.hasNestedPaths) {
    return countedLabel(group.count, "ui.filesystem_finding_group", t, params);
  }
  return countedLabel(group.count, "ui.filesystem_finding_group_root", t, params);
}

function findingKey(finding: FilesystemFinding, index: number): string {
  return `${finding.path || ""}:${finding.code}:${index}`;
}

function FindingPathRow({
  finding,
  testId = "filesystem-finding-row",
  showRemediation = false,
  showCode = false,
}: {
  finding: FilesystemFinding;
  testId?: string;
  showRemediation?: boolean;
  showCode?: boolean;
}) {
  return (
    <li data-testid={testId} className="break-words">
      <code className="text-[12px]">{finding.path || "—"}</code>
      {showCode ? (
        <>
          {" — "}
          <code className="text-[12px] text-muted">{finding.code}</code>
        </>
      ) : null}
      {" — "}
      <span>{finding.message || finding.code}</span>
      {showRemediation && finding.remediation ? (
        <>
          {" — "}
          <span className="text-muted">{finding.remediation}</span>
        </>
      ) : null}
    </li>
  );
}

function GroupSummaryCard({
  group,
  remediation,
  t,
  testId = "filesystem-finding-group",
  className,
}: {
  group: FilesystemFindingGroup;
  remediation: string | null;
  t: Translate;
  testId?: string;
  className?: string;
}) {
  return (
    <li data-testid={testId} className={className}>
      <div className="font-semibold text-ink">{groupSummaryLabel(group, t)}</div>
      {remediation ? <div className="mt-1 text-muted">{remediation}</div> : null}
    </li>
  );
}

function CodeCountList({
  counts,
  t,
}: {
  counts: Record<string, number>;
  t: Translate;
}) {
  const entries = Object.entries(counts).sort((a, b) => {
    if (b[1] !== a[1]) return b[1] - a[1];
    return a[0].localeCompare(b[0]);
  });
  if (entries.length === 0) return null;
  return (
    <ul
      className="mt-2 list-none space-y-1 p-0 text-[13px]"
      data-testid="filesystem-finding-counts"
    >
      {entries.map(([code, count]) => (
        <li key={code}>
          <code className="text-[12px]">{code}</code>
          {" — "}
          {countedLabel(count, "ui.filesystem_findings_count", t)}
        </li>
      ))}
    </ul>
  );
}

function StatusMeta({
  filesystem,
  healthStatus,
  t,
}: {
  filesystem: FilesystemHealth;
  healthStatus: string;
  t: Translate;
}) {
  const bits: string[] = [];
  if (filesystem.checked_at) {
    bits.push(
      t("ui.filesystem_health_checked_at", { when: filesystem.checked_at }),
    );
  }
  if (filesystem.revision != null) {
    bits.push(
      t("ui.filesystem_health_revision", { revision: filesystem.revision }),
    );
  }
  return (
    <div
      className="mt-1 space-y-0.5 text-[12px] text-muted"
      data-testid="filesystem-health-meta"
      data-health-status={healthStatus}
    >
      <div>{t(`ui.filesystem_health_status_${healthStatus}`)}</div>
      {bits.length > 0 ? <div>{bits.join(" · ")}</div> : null}
    </div>
  );
}

/**
 * Alarm / status banner for vault filesystem health.
 *
 * Health lifecycle (`checking` / `current` / `stale` / `failed`) is independent
 * of archive summary success. Findings are a bounded sample; totals come from
 * `findings_total` / `finding_counts` when present (#228). Progressive disclosure
 * from #222 still pages the available sample.
 */
export function FilesystemHealthBanner({
  filesystem,
  t,
  className,
}: FilesystemHealthBannerProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [findingPage, setFindingPage] = useState(0);
  const [groupPage, setGroupPage] = useState(0);

  const findings = useMemo(
    () => filesystem?.findings ?? [],
    [filesystem?.findings],
  );
  const groups = useMemo(() => groupFilesystemFindings(findings), [findings]);
  const checkRemediations = useMemo(
    () =>
      (filesystem?.checks || [])
        .map((check) => check.remediation)
        .filter((text): text is string => Boolean(text)),
    [filesystem?.checks],
  );

  if (!filesystem) {
    return null;
  }

  const healthStatus = resolveHealthStatus(filesystem);
  const findingsTotal = resolveFindingsTotal(filesystem);
  const sampleCount = findings.length;
  const isTruncated =
    filesystem.findings_truncated === true || findingsTotal > sampleCount;
  const findingCounts = filesystem.finding_counts ?? {};
  const isSourceVolumeDegraded = (filesystem.checks || []).some((check) =>
    check.code.startsWith("source_volume."),
  );

  // Pure healthy current — nothing to surface.
  if (
    healthStatus === "current" &&
    filesystem.ok &&
    findingsTotal === 0 &&
    !isSourceVolumeDegraded
  ) {
    return null;
  }

  // First-pass / in-flight check with no known problems yet.
  if (
    healthStatus === "checking" &&
    findingsTotal === 0 &&
    !isSourceVolumeDegraded &&
    filesystem.ok !== true
  ) {
    return (
      <section
        role="status"
        aria-live="polite"
        data-testid="filesystem-health"
        data-health-status="checking"
        className={cn(
          "filesystem-health mb-4 rounded-card border border-line bg-surface px-4 py-3.5",
          className,
        )}
      >
        <strong className="mb-1 block">
          {t("ui.filesystem_health_checking_title")}
        </strong>
        <span className="text-[13px] text-muted">
          {t("ui.filesystem_health_checking_detail")}
        </span>
        <StatusMeta filesystem={filesystem} healthStatus="checking" t={t} />
      </section>
    );
  }

  // Stale but still healthy: quiet refresh note, not an alarm.
  if (
    healthStatus === "stale" &&
    filesystem.ok &&
    findingsTotal === 0 &&
    !isSourceVolumeDegraded
  ) {
    return (
      <section
        role="status"
        aria-live="polite"
        data-testid="filesystem-health"
        data-health-status="stale"
        className={cn(
          "filesystem-health mb-4 rounded-card border border-line bg-surface px-4 py-3.5",
          className,
        )}
      >
        <strong className="mb-1 block">
          {t("ui.filesystem_health_stale_title")}
        </strong>
        <span className="text-[13px] text-muted">
          {t("ui.filesystem_health_stale_detail")}
        </span>
        <StatusMeta filesystem={filesystem} healthStatus="stale" t={t} />
      </section>
    );
  }

  if (healthStatus === "failed") {
    return (
      <section
        role="alert"
        data-testid="filesystem-health"
        data-health-status="failed"
        className={cn(
          "filesystem-health mb-4 rounded-card border border-red-300 bg-red-soft px-4 py-3.5",
          className,
        )}
      >
        <strong className="mb-1 block">
          {t("ui.filesystem_health_failed_title")}
        </strong>
        <span className="text-[13px] text-muted">
          {t("ui.filesystem_health_failed_detail")}
          {filesystem.error ? ` (${filesystem.error})` : null}
        </span>
        {checkRemediations.length > 0 ? (
          <ul className="mt-2.5 list-disc space-y-1 pl-4 text-[13px]">
            {checkRemediations.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        ) : null}
        <StatusMeta filesystem={filesystem} healthStatus="failed" t={t} />
      </section>
    );
  }

  // Remaining not-ok / degraded paths keep the fail-closed warn alarm.
  if (filesystem.ok && findingsTotal === 0 && !isSourceVolumeDegraded) {
    return null;
  }

  const totalGroups = groups.length;
  const inlineGroups = groups.slice(0, INLINE_GROUP_LIMIT);
  const hiddenGroupCount = Math.max(0, totalGroups - inlineGroups.length);

  // Prefer a flat sample of unique paths across groups for the compact banner.
  const inlineSample: FilesystemFinding[] = [];
  for (const group of groups) {
    for (const finding of group.findings) {
      if (inlineSample.length >= INLINE_FINDING_SAMPLE) break;
      inlineSample.push(finding);
    }
    if (inlineSample.length >= INLINE_FINDING_SAMPLE) break;
  }
  const inlineTruncated = findingsTotal > INLINE_FINDING_SAMPLE;

  const findingPageCount = Math.max(
    1,
    Math.ceil(Math.max(sampleCount, 1) / DETAILS_PAGE_SIZE),
  );
  const safeFindingPage = Math.min(findingPage, findingPageCount - 1);
  const pageFindings = findings.slice(
    safeFindingPage * DETAILS_PAGE_SIZE,
    safeFindingPage * DETAILS_PAGE_SIZE + DETAILS_PAGE_SIZE,
  );

  const groupPageCount = Math.max(
    1,
    Math.ceil(Math.max(totalGroups, 1) / DETAILS_GROUP_PAGE_SIZE),
  );
  const safeGroupPage = Math.min(groupPage, groupPageCount - 1);
  const pageGroups = groups.slice(
    safeGroupPage * DETAILS_GROUP_PAGE_SIZE,
    safeGroupPage * DETAILS_GROUP_PAGE_SIZE + DETAILS_GROUP_PAGE_SIZE,
  );

  const openDetails = () => {
    setFindingPage(0);
    setGroupPage(0);
    setDetailsOpen(true);
  };

  const closeDetails = (open: boolean) => {
    setDetailsOpen(open);
    if (!open) {
      setFindingPage(0);
      setGroupPage(0);
    }
  };

  const groupRemediation = (group: FilesystemFindingGroup): string | null =>
    group.remediation || checkRemediations[0] || null;

  const statusTone =
    healthStatus === "stale" || healthStatus === "checking"
      ? "stale"
      : "warn";

  return (
    <>
      <section
        role="alert"
        data-testid="filesystem-health"
        data-health-status={healthStatus}
        className={cn(
          "filesystem-health warn mb-4 rounded-card border border-[var(--health-warn-border)] bg-amber-soft px-4 py-3.5",
          className,
        )}
      >
        <strong className="mb-1 block">
          {isSourceVolumeDegraded
            ? t("ui.source_volume_degraded")
            : t("ui.filesystem_needs_attention")}
        </strong>
        <span className="text-[13px] text-muted">
          {isSourceVolumeDegraded
            ? t("ui.source_volume_degraded_detail")
            : t("ui.filesystem_attention_detail")}
          {filesystem.uid != null && filesystem.gid != null
            ? ` uid=${filesystem.uid} gid=${filesystem.gid}.`
            : null}
        </span>

        <StatusMeta
          filesystem={filesystem}
          healthStatus={healthStatus}
          t={t}
        />

        {Object.keys(findingCounts).length > 0 ? (
          <CodeCountList counts={findingCounts} t={t} />
        ) : null}

        {findingsTotal > 0 ? (
          <div className="mt-2.5 space-y-2.5 text-[13px]">
            <p
              className="font-semibold text-ink"
              data-testid="filesystem-findings-summary"
            >
              {findingsSummaryLabel(findingsTotal, totalGroups, t)}
              {isTruncated && totalGroups > 0
                ? ` ${t("ui.filesystem_findings_sample_note", {
                    shown: sampleCount,
                    total: findingsTotal,
                  })}`
                : null}
            </p>

            <ul
              className="list-none space-y-2.5 p-0"
              data-testid="filesystem-finding-groups"
            >
              {inlineGroups.map((group) => (
                <GroupSummaryCard
                  key={`${group.code}:${group.scope}`}
                  group={group}
                  remediation={groupRemediation(group)}
                  t={t}
                  className="rounded-[10px] border border-[var(--health-warn-border)]/60 bg-canvas/40 px-3 py-2"
                />
              ))}
            </ul>

            {hiddenGroupCount > 0 ? (
              <p className="text-muted" data-testid="filesystem-findings-more-groups">
                {countedLabel(
                  hiddenGroupCount,
                  "ui.filesystem_findings_more_groups",
                  t,
                )}
              </p>
            ) : null}

            {inlineSample.length > 0 ? (
              <ul className="list-disc space-y-1 pl-4">
                {inlineSample.map((finding, index) => (
                  <FindingPathRow
                    key={findingKey(finding, index)}
                    finding={finding}
                    // Small complete samples keep path-level remediation.
                    showRemediation={
                      !inlineTruncated && Boolean(finding.remediation)
                    }
                  />
                ))}
              </ul>
            ) : null}

            {inlineTruncated ? (
              <p className="text-muted" data-testid="filesystem-findings-truncated">
                {t("ui.filesystem_findings_truncated", {
                  shown: Math.min(inlineSample.length, INLINE_FINDING_SAMPLE),
                  total: findingsTotal,
                })}
              </p>
            ) : null}

            {sampleCount > 0 ? (
              <div className="pt-0.5">
                <Button
                  type="button"
                  variant="secondary"
                  className="min-h-11"
                  aria-expanded={detailsOpen}
                  aria-controls="filesystem-findings-dialog"
                  onClick={openDetails}
                >
                  {isTruncated
                    ? t("ui.filesystem_show_findings_sample", {
                        count: sampleCount,
                        total: findingsTotal,
                      })
                    : t("ui.filesystem_show_all_findings", {
                        count: findingsTotal,
                      })}
                </Button>
              </div>
            ) : null}
          </div>
        ) : null}

        {findingsTotal === 0 && checkRemediations.length > 0 ? (
          <ul className="mt-2.5 list-disc space-y-1 pl-4 text-[13px]">
            {checkRemediations.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        ) : null}

        {statusTone === "stale" ? (
          <p className="mt-2 text-[12px] text-muted" data-testid="filesystem-health-refreshing">
            {healthStatus === "checking"
              ? t("ui.filesystem_health_checking_detail")
              : t("ui.filesystem_health_stale_detail")}
          </p>
        ) : null}
      </section>

      <Dialog
        open={detailsOpen}
        onOpenChange={closeDetails}
        title={t("ui.filesystem_findings_details_title")}
        description={
          isTruncated
            ? t("ui.filesystem_findings_details_description_sample", {
                shown: sampleCount,
                total: findingsTotal,
              })
            : t("ui.filesystem_findings_details_description")
        }
        closeLabel={t("ui.close")}
        className="max-h-[min(85vh,720px)] overflow-hidden"
      >
        <div
          id="filesystem-findings-dialog"
          className="flex max-h-[min(65vh,560px)] flex-col gap-3"
        >
          <p className="text-sm font-semibold text-ink">
            {findingsSummaryLabel(findingsTotal, totalGroups, t)}
          </p>

          <div className="space-y-2">
            <ul
              className="max-h-40 space-y-2 overflow-y-auto pr-1 text-sm"
              data-testid="filesystem-finding-groups-detail"
            >
              {pageGroups.map((group) => (
                <GroupSummaryCard
                  key={`detail-group:${group.code}:${group.scope}`}
                  group={group}
                  remediation={groupRemediation(group)}
                  t={t}
                  testId="filesystem-finding-group-detail"
                  className="rounded-[10px] border border-line bg-canvas px-3 py-2"
                />
              ))}
            </ul>

            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-sm text-muted" data-testid="filesystem-groups-page">
                {t("ui.filesystem_groups_page", {
                  page: safeGroupPage + 1,
                  pages: groupPageCount,
                })}
              </span>
              <div className="flex gap-2">
                <Button
                  type="button"
                  variant="secondary"
                  className="min-h-11"
                  disabled={safeGroupPage <= 0}
                  aria-label={t("ui.filesystem_groups_prev_page")}
                  onClick={() =>
                    setGroupPage((current) => Math.max(0, current - 1))
                  }
                >
                  ←
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  className="min-h-11"
                  disabled={safeGroupPage >= groupPageCount - 1}
                  aria-label={t("ui.filesystem_groups_next_page")}
                  onClick={() =>
                    setGroupPage((current) =>
                      Math.min(groupPageCount - 1, current + 1),
                    )
                  }
                >
                  →
                </Button>
              </div>
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto rounded-[10px] border border-line bg-canvas">
            <ul className="list-disc space-y-1.5 p-3 pl-7 text-sm">
              {pageFindings.map((finding, index) => (
                <FindingPathRow
                  key={findingKey(
                    finding,
                    safeFindingPage * DETAILS_PAGE_SIZE + index,
                  )}
                  finding={finding}
                  testId="filesystem-finding-detail-row"
                  showCode
                  showRemediation
                />
              ))}
            </ul>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm text-muted" data-testid="filesystem-findings-page">
              {t("ui.filesystem_findings_page", {
                page: safeFindingPage + 1,
                pages: findingPageCount,
              })}
            </span>
            <div className="flex gap-2">
              <Button
                type="button"
                variant="secondary"
                className="min-h-11"
                disabled={safeFindingPage <= 0}
                aria-label={t("ui.filesystem_findings_prev_page")}
                onClick={() =>
                  setFindingPage((current) => Math.max(0, current - 1))
                }
              >
                ←
              </Button>
              <Button
                type="button"
                variant="secondary"
                className="min-h-11"
                disabled={safeFindingPage >= findingPageCount - 1}
                aria-label={t("ui.filesystem_findings_next_page")}
                onClick={() =>
                  setFindingPage((current) =>
                    Math.min(findingPageCount - 1, current + 1),
                  )
                }
              >
                →
              </Button>
            </div>
          </div>
        </div>
      </Dialog>
    </>
  );
}
