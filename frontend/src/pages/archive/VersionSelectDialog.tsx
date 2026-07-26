import type { ArchiveVersionItem } from "@/api/types";
import { Dialog } from "@/components/Dialog";
import { Button } from "@/components/ui/button";

import { formatBytes } from "./format";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type VersionSelectDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  path: string;
  versions: ArchiveVersionItem[];
  t: Translate;
  onSelect: (version: ArchiveVersionItem) => void;
};

function formatVersionDate(raw: string | null | undefined): string {
  if (!raw) return "—";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleString();
}

export function VersionSelectDialog({
  open,
  onOpenChange,
  path,
  versions,
  t,
  onSelect,
}: VersionSelectDialogProps) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("ui.select_archive_version")}
      description={t("ui.select_archive_version_description", { path })}
      className="w-[min(28rem,calc(100%-1.75rem))]"
    >
      <ul className="grid gap-2" data-testid="version-list" role="listbox">
        {versions.map((version) => {
          const storage = version.storage_class || "STANDARD";
          const label = t("ui.version_date_storage", {
            number: version.version_number,
            date: formatVersionDate(
              (version.created_at as string | null | undefined) ??
                (version.uploaded_at as string | null | undefined),
            ),
            storage,
          });
          const sizeLabel = formatBytes(version.size ?? null);
          return (
            <li key={version.id}>
              <Button
                type="button"
                variant="secondary"
                className="min-h-11 w-full justify-start px-4 text-left"
                role="option"
                data-version-id={version.id}
                data-testid={`version-option-${version.id}`}
                onClick={() => {
                  onSelect(version);
                  onOpenChange(false);
                }}
              >
                <span className="flex min-w-0 flex-col gap-0.5">
                  <span className="truncate font-bold">{label}</span>
                  <span className="truncate text-xs text-muted">
                    {t("ui.version_option", {
                      number: version.version_number,
                      storage,
                      size: sizeLabel,
                    })}
                  </span>
                </span>
              </Button>
            </li>
          );
        })}
      </ul>
    </Dialog>
  );
}
