import type { ArchiveVersionItem, ArchiveVersionSummary } from "@/api/types";
import { StorageBadge } from "@/components/StorageBadge";

import { formatArchiveDate } from "./archiveVersionPresentation";
import { storageKind, storageLabel } from "./fileLabels";
import { formatBytes } from "./format";

type Translate = (key: string, params?: Record<string, string | number>) => string;

type DisplayVersion = (ArchiveVersionItem | ArchiveVersionSummary) & {
  created_at?: string | null;
  uploaded_at?: string | null;
  storage_class_source?: string | null;
};

function storageSourceLabel(
  source: string | null | undefined,
  t: Translate,
): string | null {
  if (!source || source === "unknown") return null;
  const key = `ui.storage_class_source.${source}`;
  const label = t(key);
  return label === key ? null : label;
}

/** Shared two-line presentation used by Path History and Recover. */
export function ArchiveVersionDetails({
  version,
  t,
}: {
  version: DisplayVersion;
  t: Translate;
}) {
  const number = version.version_number ?? "—";
  const storage = version.storage_class || "STANDARD";
  const date = formatArchiveDate(version.created_at ?? version.uploaded_at);
  const source = storageSourceLabel(version.storage_class_source, t);

  return (
    <span className="flex min-w-0 flex-col gap-1">
      <span className="truncate font-bold">
        {t("ui.version_title_date", { number, date })}
      </span>
      <span className="flex min-w-0 flex-wrap items-center gap-2">
        <StorageBadge
          storage={storageKind(storage)}
          label={storageLabel(storage, t)}
        />
        <span className="text-xs font-bold text-ink">
          {formatBytes(version.size ?? null)}
        </span>
        {source ? (
          <span className="text-xs text-muted">{source}</span>
        ) : null}
      </span>
    </span>
  );
}
