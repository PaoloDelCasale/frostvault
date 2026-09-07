import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useEffect } from "react";

import { fileHistoryQueryOptions } from "@/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { ArchiveVersionDetails } from "./ArchiveVersionDetails";
import { formatArchiveDate } from "./archiveVersionPresentation";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type PathHistoryPanelProps = {
  path: string;
  t: Translate;
  onClose?: () => void;
  open?: boolean;
  onExited?: () => void;
  variant?: "sheet" | "inline";
};

/**
 * Readable Path History timeline for one Vault File (/api/file-history).
 */
export function PathHistoryPanel({
  path,
  t,
  onClose,
  open = true,
  onExited,
  variant = "sheet",
}: PathHistoryPanelProps) {
  const historyQuery = useQuery(fileHistoryQueryOptions(path));
  const history = historyQuery.data;
  const paths = history?.path_history ?? [];
  const showPathChanges = paths.length > 1;
  const versions = [...(history?.versions ?? [])].sort((left, right) => {
    const byNumber =
      (right.version_number ?? 0) - (left.version_number ?? 0);
    if (byNumber !== 0) return byNumber;
    return String(right.uploaded_at ?? "").localeCompare(
      String(left.uploaded_at ?? ""),
    );
  });

  useEffect(() => {
    if (open || !onExited) return;
    const timeout = window.setTimeout(onExited, 220);
    return () => window.clearTimeout(timeout);
  }, [onExited, open]);

  return (
    <section
      key={path}
      className={cn(
        "path-history-panel overflow-y-auto border border-line",
        variant === "sheet"
          ? "path-history-sheet fixed inset-x-0 bottom-0 z-50 max-h-[78svh] rounded-t-panel border-b-0 bg-surface px-4 pt-2 pb-[max(1rem,env(safe-area-inset-bottom))] shadow-[0_-18px_48px_var(--shadow-color)]"
          : "path-history-inline max-h-[32rem] rounded-xl bg-canvas p-4",
      )}
      data-testid={variant === "inline" ? "path-history-inline" : "path-history"}
      data-open={open ? "true" : "false"}
      data-path={path}
      data-variant={variant}
      aria-label={t("ui.path_history")}
      aria-hidden={!open || undefined}
      onAnimationEnd={() => {
        if (!open) onExited?.();
      }}
    >
      {variant === "sheet" ? (
        <div className="mx-auto mb-3 h-1 w-10 rounded-full bg-line" aria-hidden="true" />
      ) : null}
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="min-w-0 pt-1">
          <h2 className="text-base font-bold text-ink md:text-sm">{t("ui.path_history")}</h2>
          <p className="truncate text-xs text-muted">{path}</p>
        </div>
        {onClose ? (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="rounded-full"
            aria-label={t("ui.close_path_history")}
            onClick={onClose}
          >
            <X className="size-5" aria-hidden="true" />
          </Button>
        ) : null}
      </div>

      <div
        key={path}
        className={variant === "sheet" ? "path-history-sheet-content" : undefined}
      >
      {historyQuery.isLoading ? (
        <p className="text-sm text-muted">{t("ui.path_history_loading")}</p>
      ) : null}

      {historyQuery.isError ? (
        <p className="text-sm text-destructive" role="alert">
          {t("ui.path_history_error")}
        </p>
      ) : null}

      {history ? (
        <div className="grid gap-4">
          {showPathChanges ? (
            <div>
              <p className="mb-2 text-xs font-bold tracking-wide text-muted uppercase">
                {t("ui.path_changes")}
              </p>
              <ol
                className="relative ms-2 border-s border-line ps-4"
                data-testid={
                  variant === "inline"
                    ? "path-history-timeline-inline"
                    : "path-history-timeline"
                }
              >
                {paths.map((entry, index) => (
                  <li key={`${entry.path}-${index}`} className="relative mb-3 last:mb-0">
                    <span
                      className="absolute -start-[1.3rem] top-1.5 size-2.5 rounded-full bg-primary"
                      aria-hidden="true"
                    />
                    <p className="break-all text-sm font-bold text-ink">{entry.path}</p>
                    {entry.valid_from ? (
                      <p className="text-xs text-muted">
                        {formatArchiveDate(entry.valid_from)}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ol>
            </div>
          ) : null}

          <p
            className="text-xs text-muted"
            data-testid={
              variant === "inline"
                ? "path-history-versions-inline"
                : "path-history-versions"
            }
          >
            {versions.length
              ? t("ui.path_history_versions", { count: versions.length })
              : t("ui.path_history_no_versions")}
          </p>
          {versions.length > 0 ? (
            <ol
              className="grid gap-2"
              data-testid="path-history-version-list"
            >
              {versions.map((version, index) => (
                <li
                  key={`${version.version_number ?? version.object_key ?? index}`}
                  className="rounded-lg border border-input bg-surface px-4 py-3 text-left text-sm text-ink"
                >
                  <ArchiveVersionDetails version={version} t={t} />
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      ) : null}
      </div>
    </section>
  );
}
