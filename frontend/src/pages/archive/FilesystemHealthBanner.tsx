import { useMemo, useState } from "react";

import type { FilesystemFinding, FilesystemHealth } from "@/api/types";
import { Dialog } from "@/components/Dialog";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import {
  DETAILS_PAGE_SIZE,
  INLINE_FINDING_SAMPLE,
  INLINE_GROUP_LIMIT,
  groupFilesystemFindings,
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

/**
 * Alarm banner for vault filesystem health.
 * When `ok` is true there is no alarm (operators still see stats elsewhere).
 * When not ok, findings are summarized/grouped with progressive disclosure so
 * large adopted Vaults stay usable.
 */
export function FilesystemHealthBanner({
  filesystem,
  t,
  className,
}: FilesystemHealthBannerProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [page, setPage] = useState(0);

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

  if (!filesystem || filesystem.ok) {
    return null;
  }

  const isSourceVolumeDegraded = (filesystem.checks || []).some((check) =>
    check.code.startsWith("source_volume."),
  );
  const totalFindings = findings.length;
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
  const isTruncated = totalFindings > INLINE_FINDING_SAMPLE;
  const pageCount = Math.max(1, Math.ceil(totalFindings / DETAILS_PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const pageFindings = findings.slice(
    safePage * DETAILS_PAGE_SIZE,
    safePage * DETAILS_PAGE_SIZE + DETAILS_PAGE_SIZE,
  );

  const openDetails = () => {
    setPage(0);
    setDetailsOpen(true);
  };

  const closeDetails = (open: boolean) => {
    setDetailsOpen(open);
    if (!open) setPage(0);
  };

  return (
    <>
      <section
        role="alert"
        data-testid="filesystem-health"
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

        {totalFindings > 0 ? (
          <div className="mt-2.5 space-y-2.5 text-[13px]">
            <p className="font-semibold text-ink" data-testid="filesystem-findings-summary">
              {countedLabel(totalFindings, "ui.filesystem_findings_summary", t, {
                groups: totalGroups,
              })}
            </p>

            <ul
              className="list-none space-y-2.5 p-0"
              data-testid="filesystem-finding-groups"
            >
              {inlineGroups.map((group) => {
                const label =
                  group.scope && group.hasNestedPaths
                    ? t("ui.filesystem_finding_group", {
                        message: group.message,
                        code: group.code,
                        count: group.count,
                        scope: group.scope,
                      })
                    : t("ui.filesystem_finding_group_root", {
                        message: group.message,
                        code: group.code,
                        count: group.count,
                      });
                const remediation =
                  group.remediation || checkRemediations[0] || null;
                return (
                  <li
                    key={`${group.code}:${group.scope}`}
                    data-testid="filesystem-finding-group"
                    className="rounded-[10px] border border-[var(--health-warn-border)]/60 bg-canvas/40 px-3 py-2"
                  >
                    <div className="font-semibold text-ink">{label}</div>
                    {remediation ? (
                      <div className="mt-1 text-muted">{remediation}</div>
                    ) : null}
                  </li>
                );
              })}
            </ul>

            {hiddenGroupCount > 0 ? (
              <p className="text-muted">
                {t("ui.filesystem_findings_more_groups", {
                  count: hiddenGroupCount,
                })}
              </p>
            ) : null}

            {inlineSample.length > 0 ? (
              <ul className="list-disc space-y-1 pl-4">
                {inlineSample.map((finding, index) => (
                  <FindingPathRow
                    key={findingKey(finding, index)}
                    finding={finding}
                    // Small sets keep path-level remediation when each finding is unique.
                    showRemediation={!isTruncated && Boolean(finding.remediation)}
                  />
                ))}
              </ul>
            ) : null}

            {isTruncated ? (
              <p className="text-muted" data-testid="filesystem-findings-truncated">
                {t("ui.filesystem_findings_truncated", {
                  shown: inlineSample.length,
                  total: totalFindings,
                })}
              </p>
            ) : null}

            <div className="pt-0.5">
              <Button
                type="button"
                variant="secondary"
                className="min-h-11"
                aria-expanded={detailsOpen}
                aria-controls="filesystem-findings-dialog"
                onClick={openDetails}
              >
                {t("ui.filesystem_show_all_findings", { count: totalFindings })}
              </Button>
            </div>
          </div>
        ) : null}

        {totalFindings === 0 && checkRemediations.length > 0 ? (
          <ul className="mt-2.5 list-disc space-y-1 pl-4 text-[13px]">
            {checkRemediations.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        ) : null}
      </section>

      <Dialog
        open={detailsOpen}
        onOpenChange={closeDetails}
        title={t("ui.filesystem_findings_details_title")}
        description={t("ui.filesystem_findings_details_description")}
        closeLabel={t("ui.close")}
        className="max-h-[min(85vh,720px)] overflow-hidden"
      >
        <div
          id="filesystem-findings-dialog"
          className="flex max-h-[min(65vh,560px)] flex-col gap-3"
        >
          <p className="text-sm font-semibold text-ink">
            {countedLabel(totalFindings, "ui.filesystem_findings_summary", t, {
              groups: totalGroups,
            })}
          </p>

          <ul
            className="max-h-40 shrink-0 space-y-2 overflow-y-auto pr-1 text-sm"
            data-testid="filesystem-finding-groups-detail"
          >
            {groups.map((group) => {
              const label =
                group.scope && group.hasNestedPaths
                  ? t("ui.filesystem_finding_group", {
                      message: group.message,
                      code: group.code,
                      count: group.count,
                      scope: group.scope,
                    })
                  : t("ui.filesystem_finding_group_root", {
                      message: group.message,
                      code: group.code,
                      count: group.count,
                    });
              const remediation =
                group.remediation || checkRemediations[0] || null;
              return (
                <li
                  key={`detail-group:${group.code}:${group.scope}`}
                  className="rounded-[10px] border border-line bg-canvas px-3 py-2"
                >
                  <div className="font-semibold">{label}</div>
                  {remediation ? (
                    <div className="mt-1 text-muted">{remediation}</div>
                  ) : null}
                </li>
              );
            })}
          </ul>

          <div className="min-h-0 flex-1 overflow-y-auto rounded-[10px] border border-line bg-canvas">
            <ul className="list-disc space-y-1.5 p-3 pl-7 text-sm">
              {pageFindings.map((finding, index) => (
                <FindingPathRow
                  key={findingKey(finding, safePage * DETAILS_PAGE_SIZE + index)}
                  finding={finding}
                  testId="filesystem-finding-detail-row"
                  showCode
                  showRemediation
                />
              ))}
            </ul>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm text-muted">
              {t("ui.filesystem_findings_page", {
                page: safePage + 1,
                pages: pageCount,
              })}
            </span>
            <div className="flex gap-2">
              <Button
                type="button"
                variant="secondary"
                className="min-h-11"
                disabled={safePage <= 0}
                aria-label={t("ui.filesystem_findings_prev_page")}
                onClick={() => setPage((current) => Math.max(0, current - 1))}
              >
                ←
              </Button>
              <Button
                type="button"
                variant="secondary"
                className="min-h-11"
                disabled={safePage >= pageCount - 1}
                aria-label={t("ui.filesystem_findings_next_page")}
                onClick={() =>
                  setPage((current) => Math.min(pageCount - 1, current + 1))
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
