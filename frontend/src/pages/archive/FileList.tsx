import { Badge } from "@/components/Badge";
import { StorageBadge } from "@/components/StorageBadge";
import { Button } from "@/components/ui/button";
import type { ArchiveListItem } from "@/api/types";
import { formatBytes, formatCount } from "./format";
import {
  cloudStorageDisplay,
  isDirectory,
  itemSizeBytes,
  itemStateBadge,
} from "./fileLabels";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type FileListProps = {
  items: ArchiveListItem[];
  t: Translate;
  onOpenDirectory: (path: string) => void;
  onOpenFile: (path: string) => void;
  /** Placeholder for #67 — opens the actions sheet. */
  onOpenActions?: (path: string) => void;
};

function CloudStorageCell({
  item,
  t,
}: {
  item: ArchiveListItem;
  t: Translate;
}) {
  const display = cloudStorageDisplay(item, t);
  if (display.kind === "badge") {
    return <StorageBadge storage={display.storage} label={display.label} />;
  }
  if (display.kind === "summary") {
    return <span className="text-xs font-bold text-muted">{display.text}</span>;
  }
  return <span className="text-muted">—</span>;
}

function MoreActionsButton({
  path,
  t,
  onOpenActions,
}: {
  path: string;
  t: Translate;
  onOpenActions?: (path: string) => void;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className="min-h-11 min-w-11 shrink-0"
      aria-label={t("ui.more_actions")}
      onClick={(event) => {
        event.stopPropagation();
        onOpenActions?.(path);
      }}
    >
      <span aria-hidden="true" className="text-lg leading-none">
        ⋯
      </span>
    </Button>
  );
}

function ItemName({
  item,
  t,
  onOpenDirectory,
  onOpenFile,
}: {
  item: ArchiveListItem;
  t: Translate;
  onOpenDirectory: (path: string) => void;
  onOpenFile: (path: string) => void;
}) {
  if (isDirectory(item)) {
    return (
      <button
        type="button"
        className="min-h-11 max-w-full text-left"
        data-directory={item.path}
        onClick={() => onOpenDirectory(item.path)}
      >
        <span className="block truncate font-bold text-ink">{item.name}</span>
        <span className="block truncate text-xs text-muted">
          {t("ui.folder_item_count", { count: formatCount(item.item_count) })}
        </span>
      </button>
    );
  }
  return (
    <button
      type="button"
      className="min-h-11 max-w-full text-left"
      data-file-path={item.path}
      onClick={() => onOpenFile(item.path)}
    >
      <span className="block truncate font-bold text-ink">{item.name}</span>
    </button>
  );
}

function StateCell({ item, t }: { item: ArchiveListItem; t: Translate }) {
  const badge = itemStateBadge(item, t);
  if (isDirectory(item) && item.state_counts) {
    const details = Object.entries(item.state_counts)
      .filter(([, count]) => count)
      .map(([state, count]) => {
        const key = `state.${state}`;
        const label = t(key);
        return `${count} ${label === key ? state : label}`;
      })
      .join(" · ");
    return (
      <div className="flex min-w-0 flex-col gap-1">
        <Badge state={badge.state} label={badge.label} />
        {details ? (
          <span className="truncate text-xs text-muted">{details}</span>
        ) : null}
      </div>
    );
  }
  return <Badge state={badge.state} label={badge.label} />;
}

/**
 * Dual rendering: cards below `md`, table from `md` up.
 *
 * Column → card mapping:
 * - Name → title (+ folder count subtitle)
 * - Size → size line
 * - State → Badge (+ directory state detail)
 * - Cloud storage → StorageBadge / class summary
 * - Actions → ⋯ (sheet in #67)
 */
export function FileList({
  items,
  t,
  onOpenDirectory,
  onOpenFile,
  onOpenActions,
}: FileListProps) {
  return (
    <>
      <ul
        data-testid="file-list-cards"
        className="divide-y divide-line md:hidden"
      >
        {items.map((item) => {
          const size = itemSizeBytes(item);
          return (
            <li
              key={`${item.type}:${item.path}`}
              className="flex items-start gap-2 py-3 first:pt-0 last:pb-0"
              data-path={item.path}
            >
              <div className="min-w-0 flex-1">
                <ItemName
                  item={item}
                  t={t}
                  onOpenDirectory={onOpenDirectory}
                  onOpenFile={onOpenFile}
                />
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <StateCell item={item} t={t} />
                  <span className="text-sm font-bold text-ink">
                    {formatBytes(size)}
                  </span>
                  {isDirectory(item) ? (
                    <span className="text-xs text-muted">{t("ui.file_total")}</span>
                  ) : null}
                  <CloudStorageCell item={item} t={t} />
                </div>
              </div>
              <MoreActionsButton
                path={item.path}
                t={t}
                onOpenActions={onOpenActions}
              />
            </li>
          );
        })}
      </ul>

      <div data-testid="file-list-table" className="hidden md:block">
        <table className="w-full border-collapse text-left text-sm">
          <thead>
            <tr className="border-b border-line text-xs uppercase tracking-wide text-muted">
              <th className="py-2 pr-3 font-bold">{t("ui.name")}</th>
              <th className="py-2 pr-3 font-bold">{t("ui.size")}</th>
              <th className="py-2 pr-3 font-bold">{t("ui.state")}</th>
              <th className="py-2 pr-3 font-bold">{t("ui.cloud_storage")}</th>
              <th className="py-2 font-bold">
                <span className="sr-only">{t("ui.more_actions")}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const size = itemSizeBytes(item);
              return (
                <tr
                  key={`${item.type}:${item.path}`}
                  className="border-b border-line last:border-b-0"
                  data-path={item.path}
                >
                  <td className="max-w-[16rem] py-2 pr-3 align-middle">
                    <ItemName
                      item={item}
                      t={t}
                      onOpenDirectory={onOpenDirectory}
                      onOpenFile={onOpenFile}
                    />
                  </td>
                  <td className="whitespace-nowrap py-2 pr-3 align-middle">
                    <span className="font-bold">{formatBytes(size)}</span>
                    {isDirectory(item) ? (
                      <span className="mt-0.5 block text-xs text-muted">
                        {t("ui.file_total")}
                      </span>
                    ) : null}
                  </td>
                  <td className="py-2 pr-3 align-middle">
                    <StateCell item={item} t={t} />
                  </td>
                  <td className="py-2 pr-3 align-middle">
                    <CloudStorageCell item={item} t={t} />
                  </td>
                  <td className="py-2 align-middle">
                    <MoreActionsButton
                      path={item.path}
                      t={t}
                      onOpenActions={onOpenActions}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
