import { useEffect, useId, useRef, useState, type ComponentType } from "react";
import { createPortal } from "react-dom";
import {
  ChevronDown,
  ChevronUp,
  File,
  FileArchive,
  FileAudio,
  FileCode,
  FileImage,
  FileSpreadsheet,
  FileText,
  FileType,
  FileVideo,
  Folder,
  Presentation,
} from "lucide-react";

import { Badge, type BadgeState } from "@/components/Badge";
import { StorageBadge } from "@/components/StorageBadge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ArchiveListItem, JobGroup } from "@/api/types";

import {
  actionLabel,
  availableActions,
  groupRowActions,
  partitionRowActions,
  ROW_ACTION_CATEGORY_LABEL_KEYS,
  type RowAction,
  type RowActionId,
  type VaultCapabilities,
} from "./actions";
import { formatBytes, formatCompactCount, formatCount } from "./format";
import { fileKindForItem, type FileKind } from "./fileKind";
import type { FileSortKey, FileSortOrder } from "./fileSort";
import {
  cloudStorageDisplay,
  isDirectory,
  itemSizeBytes,
  itemStateBadge,
} from "./fileLabels";
import { JobProgress } from "./JobProgress";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type FileListProps = {
  items: ArchiveListItem[];
  t: Translate;
  capabilities: VaultCapabilities;
  onOpenDirectory: (path: string) => void;
  onOpenFile: (path: string) => void;
  sortKey?: FileSortKey;
  sortOrder?: FileSortOrder;
  onSort?: (key: FileSortKey) => void;
  /** Mobile: opens the bottom sheet of row actions. */
  onOpenActions?: (path: string) => void;
  /** Desktop: run an action inline. */
  onDesktopAction?: (path: string, action: RowActionId) => void;
  jobsByPath?: Map<string, JobGroup[]>;
  onCancelJob?: (job: JobGroup) => void;
  onApproveJob?: (job: JobGroup) => void;
  onAcceleratePurge?: (job: JobGroup) => void;
  cancelBusyId?: string | null;
  approveBusyId?: string | null;
  accelerateBusyId?: string | null;
};

function isDeepArchiveRow(item: ArchiveListItem): boolean {
  return (
    item.type === "file" &&
    Boolean(item.cloud_exists) &&
    item.storage_class === "DEEP_ARCHIVE"
  );
}

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
  expanded,
  testId,
}: {
  path: string;
  t: Translate;
  onOpenActions?: (path: string) => void;
  expanded?: boolean;
  testId?: string;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className="min-h-11 min-w-11 shrink-0"
      aria-label={t("ui.more_actions")}
      aria-haspopup={expanded === undefined ? undefined : "menu"}
      aria-expanded={expanded}
      data-testid={testId ?? `more-actions-${path}`}
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

function ActionButton({
  action,
  item,
  t,
  onDesktopAction,
  className,
}: {
  action: RowAction;
  item: ArchiveListItem;
  t: Translate;
  onDesktopAction?: (path: string, action: RowActionId) => void;
  className?: string;
}) {
  return (
    <Button
      type="button"
      variant={action.tone === "danger" ? "danger" : "secondary"}
      className={cn("min-h-11 min-w-11 px-3", className)}
      data-action={action.id}
      data-path={item.path}
      data-is-directory={isDirectory(item) ? "true" : "false"}
      onClick={() => onDesktopAction?.(item.path, action.id)}
    >
      {actionLabel(action.id, t, {
        count: action.count,
        isDirectory: isDirectory(item),
      })}
    </Button>
  );
}

function OverflowMenu({
  item,
  actions,
  t,
  onDesktopAction,
}: {
  item: ArchiveListItem;
  actions: RowAction[];
  t: Translate;
  onDesktopAction?: (path: string, action: RowActionId) => void;
}) {
  const menuId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState<{ top: number; right: number } | null>(
    null,
  );
  const groups = groupRowActions(actions);

  useEffect(() => {
    if (!open) return;
    const trigger = rootRef.current?.querySelector("button");
    if (trigger) {
      const rect = trigger.getBoundingClientRect();
      setCoords({
        top: rect.bottom + 4,
        right: window.innerWidth - rect.right,
      });
    }
    function onPointerDown(event: MouseEvent) {
      const target = event.target as Node;
      if (rootRef.current?.contains(target) || menuRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={rootRef} className="relative">
      <MoreActionsButton
        path={item.path}
        t={t}
        expanded={open}
        testId={`more-actions-desktop-${item.path}`}
        onOpenActions={() => setOpen((next) => !next)}
      />
      {open && coords && typeof document !== "undefined"
        ? createPortal(
            <div
              ref={menuRef}
              id={menuId}
              role="menu"
              aria-label={t("ui.more_actions")}
              className="fixed z-[80] min-w-[16rem] overflow-auto rounded-[10px] border border-line bg-surface p-1 text-ink shadow-lg"
              style={{ top: coords.top, right: coords.right }}
            >
              {groups.map((group) => (
                <div key={group.category} className="py-1">
                  <p className="px-3 py-1.5 text-xs font-bold tracking-wide text-muted uppercase">
                    {t(ROW_ACTION_CATEGORY_LABEL_KEYS[group.category])}
                  </p>
                  {group.actions.map((action) => (
                    <button
                      key={action.id}
                      type="button"
                      role="menuitem"
                      data-action={action.id}
                      data-path={item.path}
                      className={cn(
                        "flex min-h-11 w-full items-center rounded-lg px-3 text-left text-sm font-bold outline-none",
                        "focus-visible:bg-canvas focus-visible:ring-2 focus-visible:ring-ring/40",
                        action.tone === "danger"
                          ? "text-[var(--state-local-fg)]"
                          : "text-ink",
                      )}
                      onClick={() => {
                        setOpen(false);
                        onDesktopAction?.(item.path, action.id);
                      }}
                    >
                      {actionLabel(action.id, t, {
                        count: action.count,
                        isDirectory: isDirectory(item),
                      })}
                    </button>
                  ))}
                </div>
              ))}
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}

function DesktopActions({
  item,
  t,
  capabilities,
  onDesktopAction,
}: {
  item: ArchiveListItem;
  t: Translate;
  capabilities: VaultCapabilities;
  onDesktopAction?: (path: string, action: RowActionId) => void;
}) {
  const actions = availableActions(item, capabilities);
  if (!actions.length) return null;
  const { primary, overflow } = partitionRowActions(actions);
  return (
    <div
      className="row-actions compact flex flex-wrap items-center justify-end gap-1"
      data-testid={`desktop-actions-${item.path}`}
    >
      {primary.map((action) => (
        <ActionButton
          key={action.id}
          action={action}
          item={item}
          t={t}
          onDesktopAction={onDesktopAction}
        />
      ))}
      {overflow.length ? (
        <OverflowMenu
          item={item}
          actions={overflow}
          t={t}
          onDesktopAction={onDesktopAction}
        />
      ) : null}
    </div>
  );
}

const FILE_KIND_ICON: Record<FileKind, ComponentType<{ className?: string }>> = {
  folder: Folder,
  pdf: FileType,
  image: FileImage,
  video: FileVideo,
  audio: FileAudio,
  archive: FileArchive,
  spreadsheet: FileSpreadsheet,
  presentation: Presentation,
  code: FileCode,
  text: FileText,
  file: File,
};

function EntryIcon({ item }: { item: ArchiveListItem }) {
  const kind = fileKindForItem(item);
  const Icon = FILE_KIND_ICON[kind];
  return (
    <Icon
      className="size-4 shrink-0 text-muted"
      aria-hidden
      data-file-kind={kind}
    />
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
        className="flex min-h-11 max-w-full items-center gap-2.5 text-left"
        data-directory={item.path}
        onClick={() => onOpenDirectory(item.path)}
      >
        <EntryIcon item={item} />
        <span className="min-w-0">
          <span className="block truncate font-bold text-ink">{item.name}</span>
          <span className="block truncate text-xs text-muted">
            {t("ui.folder_item_count", { count: formatCount(item.item_count) })}
          </span>
        </span>
      </button>
    );
  }
  return (
    <button
      type="button"
      className="flex min-h-11 max-w-full items-center gap-2.5 text-left"
      data-file-path={item.path}
      onClick={() => onOpenFile(item.path)}
    >
      <EntryIcon item={item} />
      <span className="block min-w-0 truncate font-semibold text-ink">
        {item.name}
      </span>
    </button>
  );
}

const STATE_COUNT_ORDER: BadgeState[] = [
  "both",
  "local_only",
  "cloud_only",
  "restoring",
  "missing",
  "unsupported",
];

function StateCell({ item, t }: { item: ArchiveListItem; t: Translate }) {
  const badge = itemStateBadge(item, t);
  if (isDirectory(item) && item.state_counts) {
    const breakdown = STATE_COUNT_ORDER.flatMap((state) => {
      const count = item.state_counts?.[state];
      if (!count) return [];
      const key = `state.${state}`;
      const label = t(key);
      const stateLabel = label === key ? state : label;
      return [
        {
          state,
          label: `${formatCompactCount(count)} ${stateLabel}`,
          title: `${formatCount(count)} ${stateLabel}`,
        },
      ];
    });
    return (
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <Badge state={badge.state} label={badge.label} />
        {breakdown.map((entry) => (
          <span key={entry.state} title={entry.title}>
            <Badge state={entry.state} size="sm" label={entry.label} />
          </span>
        ))}
      </div>
    );
  }
  return <Badge state={badge.state} label={badge.label} />;
}

function RowJobOrActions({
  item,
  t,
  capabilities,
  jobs,
  onOpenActions,
  onDesktopAction,
  onCancelJob,
  onApproveJob,
  onAcceleratePurge,
  cancelBusyId,
  approveBusyId,
  accelerateBusyId,
  layout,
}: {
  item: ArchiveListItem;
  t: Translate;
  capabilities: VaultCapabilities;
  jobs: JobGroup[];
  onOpenActions?: (path: string) => void;
  onDesktopAction?: (path: string, action: RowActionId) => void;
  onCancelJob?: (job: JobGroup) => void;
  onApproveJob?: (job: JobGroup) => void;
  onAcceleratePurge?: (job: JobGroup) => void;
  cancelBusyId?: string | null;
  approveBusyId?: string | null;
  accelerateBusyId?: string | null;
  layout: "card" | "table";
}) {
  if (jobs.length) {
    return (
      <div className="progress-stack flex min-w-[190px] flex-col gap-2">
        {jobs.map((job) => {
          const cloudDeletionJob =
            job.action === "cloud-purge" || job.action === "cloud-archive";
          return (
            <JobProgress
              key={job.id}
              job={job}
              t={t}
              canCancel={
                cloudDeletionJob
                  ? capabilities.is_vault_owner
                  : capabilities.can_operate
              }
              canApprove={capabilities.is_vault_owner}
              canAcceleratePurge={capabilities.is_vault_owner}
              onCancel={(j) => onCancelJob?.(j)}
              onApprove={(j) => onApproveJob?.(j)}
              onAcceleratePurge={(j) => onAcceleratePurge?.(j)}
              cancelBusy={cancelBusyId === job.id}
              approveBusy={approveBusyId === job.id}
              accelerateBusy={accelerateBusyId === job.id}
            />
          );
        })}
      </div>
    );
  }
  if (layout === "card") {
    if (!availableActions(item, capabilities).length) return null;
    return (
      <MoreActionsButton path={item.path} t={t} onOpenActions={onOpenActions} />
    );
  }
  return (
    <DesktopActions
      item={item}
      t={t}
      capabilities={capabilities}
      onDesktopAction={onDesktopAction}
    />
  );
}

/**
 * Dual rendering: cards below `md`, table from `md` up.
 *
 * Column → card mapping:
 * - Name → title (+ folder count subtitle)
 * - Size → size line
 * - State → Badge (+ directory state detail)
 * - Cloud storage → StorageBadge / class summary
 * - Actions → ⋯ bottom sheet (mobile) / inline buttons (desktop)
 */
function SortableHeader({
  column,
  label,
  sortKey,
  sortOrder,
  onSort,
  className,
  t,
}: {
  column: FileSortKey;
  label: string;
  sortKey: FileSortKey;
  sortOrder: FileSortOrder;
  onSort?: (key: FileSortKey) => void;
  className?: string;
  t: Translate;
}) {
  const active = sortKey === column;
  const ariaSort = active
    ? sortOrder === "asc"
      ? "ascending"
      : "descending"
    : "none";
  return (
    <th className={cn("py-2 pr-3 font-bold", className)} aria-sort={ariaSort}>
      <button
        type="button"
        className="inline-flex min-h-11 items-center gap-1 uppercase tracking-wide text-muted outline-none focus-visible:text-ink"
        aria-label={t("ui.sort_by", { column: label })}
        onClick={() => onSort?.(column)}
      >
        {label}
        {active && sortOrder === "asc" ? (
          <ChevronUp className="size-3.5" aria-hidden />
        ) : (
          <ChevronDown
            className={cn("size-3.5", !active && "opacity-40")}
            aria-hidden
          />
        )}
      </button>
    </th>
  );
}

export function FileList({
  items,
  t,
  capabilities,
  onOpenDirectory,
  onOpenFile,
  sortKey = "name",
  sortOrder = "asc",
  onSort,
  onOpenActions,
  onDesktopAction,
  jobsByPath,
  onCancelJob,
  onApproveJob,
  onAcceleratePurge,
  cancelBusyId,
  approveBusyId,
  accelerateBusyId,
}: FileListProps) {
  return (
    <>
      <ul
        data-testid="file-list-cards"
        className="divide-y divide-line md:hidden"
      >
        {items.map((item) => {
          const size = itemSizeBytes(item);
          const deep = isDeepArchiveRow(item);
          const jobs = jobsByPath?.get(item.path) ?? [];
          return (
            <li
              key={`${item.type}:${item.path}`}
              className={
                deep
                  ? "deep-archive-row flex items-start gap-2 border-l-4 border-[var(--deep-archive-accent)] bg-[var(--deep-archive-row)] py-3 pl-2 first:pt-0 last:pb-0"
                  : "flex items-start gap-2 py-3 first:pt-0 last:pb-0"
              }
              data-path={item.path}
              data-deep-archive={deep ? "true" : undefined}
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
                  {item.lifecycle_pinned ||
                  (item.type === "directory" && item.lifecycle_pinned_partial) ? (
                    <span
                      className="rounded border border-line px-1.5 py-0.5 text-xs text-muted"
                      data-testid="lifecycle-pinned-badge"
                    >
                      {t("ui.lifecycle_pinned_badge")}
                      {item.type === "directory" && item.lifecycle_pinned_partial
                        ? "…"
                        : ""}
                    </span>
                  ) : null}
                </div>
                {jobs.length ? (
                  <div className="mt-2">
                    <RowJobOrActions
                      item={item}
                      t={t}
                      capabilities={capabilities}
                      jobs={jobs}
                      onCancelJob={onCancelJob}
                      onApproveJob={onApproveJob}
                      onAcceleratePurge={onAcceleratePurge}
                      cancelBusyId={cancelBusyId}
                      approveBusyId={approveBusyId}
                      accelerateBusyId={accelerateBusyId}
                      layout="card"
                    />
                  </div>
                ) : null}
              </div>
              {!jobs.length && availableActions(item, capabilities).length ? (
                <MoreActionsButton
                  path={item.path}
                  t={t}
                  onOpenActions={onOpenActions}
                />
              ) : null}
            </li>
          );
        })}
      </ul>

      <div data-testid="file-list-table" className="hidden md:block">
        <table className="w-full border-collapse text-left text-sm">
          <thead>
            <tr className="border-b border-line text-xs uppercase tracking-wide text-muted">
              <SortableHeader
                column="name"
                label={t("ui.name")}
                sortKey={sortKey}
                sortOrder={sortOrder}
                onSort={onSort}
                className="pl-3"
                t={t}
              />
              <SortableHeader
                column="size"
                label={t("ui.size")}
                sortKey={sortKey}
                sortOrder={sortOrder}
                onSort={onSort}
                t={t}
              />
              <th className="py-2 pr-3 font-bold uppercase tracking-wide text-muted">
                {t("ui.state")}
              </th>
              <th className="py-2 pr-3 font-bold uppercase tracking-wide text-muted">
                {t("ui.cloud_storage")}
              </th>
              <th className="py-2 font-bold">
                <span className="sr-only">{t("ui.more_actions")}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const size = itemSizeBytes(item);
              const deep = isDeepArchiveRow(item);
              const jobs = jobsByPath?.get(item.path) ?? [];
              return (
                <tr
                  key={`${item.type}:${item.path}`}
                  className={
                    deep
                      ? "deep-archive-row border-b border-line bg-[var(--deep-archive-row)] last:border-b-0 [&>td:first-child]:shadow-[inset_4px_0_0_0_var(--deep-archive-accent)]"
                      : "border-b border-line last:border-b-0"
                  }
                  data-path={item.path}
                  data-deep-archive={deep ? "true" : undefined}
                >
                  <td className="max-w-[16rem] py-2 pl-3 pr-3 align-middle">
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
                    <div className="flex flex-wrap items-center gap-2">
                      <CloudStorageCell item={item} t={t} />
                      {item.lifecycle_pinned ||
                      (item.type === "directory" && item.lifecycle_pinned_partial) ? (
                        <span
                          className="rounded border border-line px-1.5 py-0.5 text-xs text-muted"
                          data-testid="lifecycle-pinned-badge"
                        >
                          {t("ui.lifecycle_pinned_badge")}
                          {item.type === "directory" && item.lifecycle_pinned_partial
                            ? "…"
                            : ""}
                        </span>
                      ) : null}
                    </div>
                  </td>
                  <td className="py-2 align-middle">
                    <RowJobOrActions
                      item={item}
                      t={t}
                      capabilities={capabilities}
                      jobs={jobs}
                      onOpenActions={onOpenActions}
                      onDesktopAction={onDesktopAction}
                      onCancelJob={onCancelJob}
                      onApproveJob={onApproveJob}
                      onAcceleratePurge={onAcceleratePurge}
                      cancelBusyId={cancelBusyId}
                      approveBusyId={approveBusyId}
                      accelerateBusyId={accelerateBusyId}
                      layout="table"
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
