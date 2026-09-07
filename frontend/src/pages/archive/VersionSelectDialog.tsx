import type { ArchiveVersionItem } from "@/api/types";
import { Dialog } from "@/components/Dialog";
import { Button } from "@/components/ui/button";

import { ArchiveVersionDetails } from "./ArchiveVersionDetails";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type VersionSelectDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  path: string;
  versions: ArchiveVersionItem[];
  t: Translate;
  onSelect: (version: ArchiveVersionItem) => void;
};

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
                <ArchiveVersionDetails version={version} t={t} />
              </Button>
            </li>
          );
        })}
      </ul>
    </Dialog>
  );
}
