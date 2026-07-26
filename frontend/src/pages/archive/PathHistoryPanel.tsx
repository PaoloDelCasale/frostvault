import { useQuery } from "@tanstack/react-query";

import { fileHistoryQueryOptions } from "@/api";
import { Button } from "@/components/ui/button";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type PathHistoryPanelProps = {
  path: string;
  t: Translate;
  onClose: () => void;
};

/**
 * Readable Path History timeline for one Vault File (/api/file-history).
 */
export function PathHistoryPanel({ path, t, onClose }: PathHistoryPanelProps) {
  const historyQuery = useQuery(fileHistoryQueryOptions(path));
  const history = historyQuery.data;
  const paths = history?.path_history ?? [];
  const versions = history?.versions ?? [];

  return (
    <section
      className="mt-4 rounded-lg border border-line bg-canvas p-4"
      data-testid="path-history"
      aria-label={t("ui.path_history")}
    >
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-sm font-bold text-ink">{t("ui.path_history")}</h2>
          <p className="truncate text-xs text-muted">{path}</p>
        </div>
        <Button type="button" variant="ghost" size="sm" onClick={onClose}>
          {t("ui.close_path_history")}
        </Button>
      </div>

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
          <ol
            className="relative ms-2 border-s border-line ps-4"
            data-testid="path-history-timeline"
          >
            {(paths.length ? paths : [{ path: history.path }]).map((entry, index) => (
              <li key={`${entry.path}-${index}`} className="relative mb-3 last:mb-0">
                <span
                  className="absolute -start-[1.3rem] top-1.5 size-2.5 rounded-full bg-primary"
                  aria-hidden="true"
                />
                <p className="break-all text-sm font-bold text-ink">{entry.path}</p>
                {entry.valid_from ? (
                  <p className="text-xs text-muted">{entry.valid_from}</p>
                ) : null}
              </li>
            ))}
          </ol>

          <p className="text-xs text-muted" data-testid="path-history-versions">
            {versions.length
              ? t("ui.path_history_versions", { count: versions.length })
              : t("ui.path_history_no_versions")}
          </p>
          {versions.length > 0 ? (
            <ol className="grid gap-1 text-xs text-muted">
              {versions.map((version, index) => (
                <li key={`${version.object_key ?? index}`} className="break-all">
                  {version.object_key ?? "—"}
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
