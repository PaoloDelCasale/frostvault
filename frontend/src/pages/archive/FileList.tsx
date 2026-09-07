import {
  Fragment,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ComponentType,
} from "react";
import { createPortal } from "react-dom";
import { Dialog, Popover } from "radix-ui";
import {
  ChevronDown,
  ChevronRight,
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
  X,
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
import { formatBytes, formatCount } from "./format";
import { fileKindForItem, type FileKind } from "./fileKind";
import type { FileSortKey, FileSortOrder } from "./fileSort";
import {
  cloudStorageDisplay,
  isDirectory,
  itemSizeBytes,
  itemStateBadge,
} from "./fileLabels";
import { JobProgress } from "./JobProgress";
import { PathHistoryPanel } from "./PathHistoryPanel";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type FileListProps = {
  items: ArchiveListItem[];
  t: Translate;
  capabilities: VaultCapabilities;
  onOpenDirectory: (path: string) => void;
  onOpenFile: (path: string) => void;
  /** File whose Path History is actively open and highlighted. */
  selectedFilePath?: string | null;
  /** File detail retained briefly while its closing animation runs. */
  renderedFilePath?: string | null;
  onCloseFile?: () => void;
  onFileDetailExited?: () => void;
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
  showLabel = false,
}: {
  path: string;
  t: Translate;
  onOpenActions?: (path: string) => void;
  expanded?: boolean;
  testId?: string;
  showLabel?: boolean;
}) {
  const label = showLabel ? t("ui.actions") : t("ui.more_actions");
  return (
    <Button
      type="button"
      variant={showLabel ? "secondary" : "ghost"}
      size={showLabel ? "default" : "icon"}
      className={cn(
        "min-h-11 min-w-11 shrink-0",
        showLabel && "gap-1.5 whitespace-nowrap px-3",
      )}
      aria-label={label}
      aria-haspopup={expanded === undefined ? undefined : "menu"}
      aria-expanded={expanded}
      data-testid={testId ?? `more-actions-${path}`}
      onClick={(event) => {
        event.stopPropagation();
        onOpenActions?.(path);
      }}
    >
      {showLabel ? (
        <>
          <span>{label}</span>
          <ChevronDown
            className={cn(
              "size-4 transition-transform motion-reduce:transition-none",
              expanded && "rotate-180",
            )}
            aria-hidden="true"
          />
        </>
      ) : (
        <span aria-hidden="true" className="text-lg leading-none">
          ⋯
        </span>
      )}
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
  showTriggerLabel = false,
}: {
  item: ArchiveListItem;
  actions: RowAction[];
  t: Translate;
  onDesktopAction?: (path: string, action: RowActionId) => void;
  showTriggerLabel?: boolean;
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
        showLabel={showTriggerLabel}
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
                        "flex min-h-11 w-full items-center rounded-lg px-3 text-left text-sm font-bold outline-none transition-colors duration-150",
                        "hover:bg-green-soft focus-visible:bg-green-soft focus-visible:ring-2 focus-visible:ring-ring/40 motion-reduce:transition-none",
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

function DirectoryActions({
  item,
  t,
  capabilities,
  onDesktopAction,
  compact,
}: {
  item: ArchiveListItem;
  t: Translate;
  capabilities: VaultCapabilities;
  onDesktopAction?: (path: string, action: RowActionId) => void;
  compact: boolean;
}) {
  const actions = availableActions(item, capabilities);
  const { primary, overflow } = partitionRowActions(actions);

  if (!actions.length) return null;
  return (
    <div
      className="directory-actions flex min-w-11 items-center justify-end gap-1"
      data-testid={`desktop-actions-${item.path}`}
      data-layout={compact ? "compact" : "wide"}
    >
      {compact ? (
        <OverflowMenu
          item={item}
          actions={actions}
          t={t}
          onDesktopAction={onDesktopAction}
          showTriggerLabel
        />
      ) : (
        <>
          {primary.map((action) => (
            <ActionButton
              key={action.id}
              action={action}
              item={item}
              t={t}
              onDesktopAction={onDesktopAction}
              className="whitespace-nowrap"
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
        </>
      )}
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
      className="size-4 shrink-0 text-muted transition-[color,transform] duration-150 group-hover/entry:scale-110 group-hover/entry:text-ink group-focus-visible/entry:scale-110 group-focus-visible/entry:text-ink motion-reduce:transition-none"
      aria-hidden
      data-file-kind={kind}
    />
  );
}

function ItemNameContent({
  item,
  t,
}: {
  item: ArchiveListItem;
  t: Translate;
}) {
  return (
    <>
      <EntryIcon item={item} />
      <span className="min-w-0">
        <span className={cn(
          "block truncate text-ink",
          isDirectory(item) ? "font-bold" : "font-semibold",
        )}>
          {item.name}
        </span>
        {isDirectory(item) ? (
          <span className="block truncate text-xs text-muted">
            {t("ui.folder_item_count", { count: formatCount(item.item_count) })}
          </span>
        ) : null}
      </span>
    </>
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
        className="group/entry flex min-h-11 max-w-full items-center gap-2.5 rounded-lg text-left outline-none transition-colors duration-150 hover:text-ink hover:underline hover:decoration-green hover:decoration-2 hover:underline-offset-4 focus-visible:text-ink focus-visible:underline focus-visible:decoration-green focus-visible:decoration-2 focus-visible:underline-offset-4 motion-reduce:transition-none"
        data-directory={item.path}
        onClick={() => onOpenDirectory(item.path)}
      >
        <ItemNameContent item={item} t={t} />
      </button>
    );
  }
  return (
    <button
      type="button"
      className="group/entry flex min-h-11 max-w-full items-center gap-2.5 rounded-lg text-left outline-none transition-colors duration-150 hover:text-ink hover:underline hover:decoration-green hover:decoration-2 hover:underline-offset-4 focus-visible:text-ink focus-visible:underline focus-visible:decoration-green focus-visible:decoration-2 focus-visible:underline-offset-4 motion-reduce:transition-none"
      data-file-path={item.path}
      onClick={() => onOpenFile(item.path)}
    >
      <ItemNameContent item={item} t={t} />
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

type DirectoryStateBreakdownEntry = {
  state: BadgeState;
  label: string;
  count: number;
};

// Miniature two-tone versions of the primary state Badges: the fill uses
// the Badge background and the inset outline uses its foreground colour.
const STATE_DOT_CLASSES: Record<BadgeState, string> = {
  both:
    "bg-[var(--state-both-bg)] ring-1 ring-inset ring-[var(--state-both-fg)]/50",
  local_only:
    "bg-[var(--state-local-bg)] ring-1 ring-inset ring-[var(--state-local-fg)]/50",
  cloud_only:
    "bg-[var(--state-cloud-bg)] ring-1 ring-inset ring-[var(--state-cloud-fg)]/50",
  restoring:
    "bg-[var(--state-restoring-bg)] ring-1 ring-inset ring-[var(--state-restoring-fg)]/50",
  mixed:
    "bg-[var(--state-mixed-bg)] ring-1 ring-inset ring-[var(--state-mixed-fg)]/50",
  missing:
    "bg-[var(--state-missing-bg)] ring-1 ring-inset ring-[var(--state-missing-fg)]/50",
  unsupported:
    "bg-[var(--state-unsupported-bg)] ring-1 ring-inset ring-[var(--state-unsupported-fg)]/50",
};

function directoryStateBreakdown(
  item: ArchiveListItem,
  t: Translate,
): DirectoryStateBreakdownEntry[] {
  if (!isDirectory(item) || !item.state_counts) return [];
  return STATE_COUNT_ORDER.flatMap((state) => {
    const count = item.state_counts?.[state];
    if (!count) return [];
    const key = `state.${state}`;
    const translated = t(key);
    const stateLabel = translated === key ? state : translated;
    return [{ state, label: stateLabel, count }];
  });
}

function StateBreakdownList({
  entries,
  total,
  t,
}: {
  entries: DirectoryStateBreakdownEntry[];
  total?: number | null;
  t: Translate;
}) {
  return (
    <div className="grid gap-3">
      <div className="flex items-baseline justify-between gap-3 border-b border-line pb-2">
        <span className="text-sm font-bold text-ink">{t("ui.files_by_state")}</span>
        {typeof total === "number" ? (
          <span className="text-sm font-bold text-muted">
            {t("ui.state_file_count", { count: formatCount(total) })}
          </span>
        ) : null}
      </div>
      <ul className="grid gap-2" role="list">
        {entries.map((entry) => (
          <li key={entry.state} className="flex min-h-10 items-center justify-between gap-4">
            <span className="flex min-w-0 items-center gap-2 text-sm font-semibold text-ink">
              <span
                className={cn(
                  "size-3 shrink-0 rounded-full",
                  STATE_DOT_CLASSES[entry.state],
                )}
                aria-hidden="true"
              />
              <span className="truncate">{entry.label}</span>
            </span>
            <strong className="text-sm text-ink">{formatCount(entry.count)}</strong>
          </li>
        ))}
      </ul>
    </div>
  );
}

function DirectoryStateControl({
  item,
  t,
  mobile = false,
}: {
  item: ArchiveListItem;
  t: Translate;
  mobile?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const badge = itemStateBadge(item, t);
  const entries = directoryStateBreakdown(item, t);
  const total = typeof item.item_count === "number" ? item.item_count : null;
  if (!entries.length) return <Badge state={badge.state} label={badge.label} />;

  const dots = (
    <span className="flex -space-x-0.5" aria-hidden="true">
      {entries.slice(0, 3).map((entry) => (
        <span
          key={entry.state}
          className={cn(
            "size-3 rounded-full",
            STATE_DOT_CLASSES[entry.state],
          )}
        />
      ))}
    </span>
  );

  if (mobile) {
    return (
      <Dialog.Root open={open} onOpenChange={setOpen}>
        <Dialog.Trigger asChild>
          <button
            type="button"
            className="relative z-10 flex min-h-11 w-full max-w-full items-center gap-2 rounded-lg border border-line bg-canvas px-3 text-left outline-none transition-colors hover:border-[var(--interactive-border-hover)] hover:bg-green-soft/35 focus-visible:ring-2 focus-visible:ring-ring/40 motion-reduce:transition-none"
            aria-label={`${badge.label}. ${t("ui.view_state_details")}`}
            data-testid={`state-details-mobile-${item.path}`}
            onClick={(event) => event.stopPropagation()}
          >
            {dots}
            <span className="truncate text-sm font-bold text-ink">{badge.label}</span>
            <span className="ml-auto shrink-0 text-xs font-bold text-green">
              {t("ui.view_details")}
            </span>
            <ChevronRight className="size-4 shrink-0 text-green" aria-hidden="true" />
          </button>
        </Dialog.Trigger>
        <Dialog.Portal>
          <Dialog.Content
            className="path-history-sheet fixed inset-x-0 bottom-0 z-50 max-h-[78svh] overflow-y-auto rounded-t-panel border border-b-0 border-line bg-surface px-4 pt-2 pb-[max(1rem,env(safe-area-inset-bottom))] shadow-[0_-18px_48px_var(--shadow-color)] outline-none"
            aria-describedby={undefined}
            data-open={open ? "true" : "false"}
            data-testid={`state-details-sheet-${item.path}`}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="mx-auto mb-3 h-1 w-10 rounded-full bg-line" aria-hidden="true" />
            <div className="mb-3 flex items-start justify-between gap-2">
              <div className="min-w-0 pt-1">
                <Dialog.Title className="text-base font-bold text-ink">
                  {t("ui.state_details")}
                </Dialog.Title>
                <p className="truncate text-xs text-muted">{item.path}</p>
              </div>
              <Dialog.Close asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="rounded-full"
                  aria-label={t("ui.close")}
                >
                  <X className="size-5" aria-hidden="true" />
                </Button>
              </Dialog.Close>
            </div>
            <div className="path-history-sheet-content">
              <StateBreakdownList entries={entries} total={total} t={t} />
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    );
  }

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="inline-flex max-w-full items-center gap-[7px] rounded-badge bg-[var(--state-mixed-bg)] px-2.5 py-1.5 text-[13px] font-bold whitespace-nowrap text-[var(--state-mixed-fg)] outline-none ring-1 ring-transparent transition-[box-shadow,filter] hover:brightness-110 hover:ring-[var(--state-mixed-fg)]/25 focus-visible:ring-2 focus-visible:ring-ring/50 motion-reduce:transition-none"
          aria-label={`${badge.label}. ${t("ui.view_state_details")}`}
          data-testid={`state-details-desktop-${item.path}`}
        >
          {dots}
          <span className="truncate">{badge.label}</span>
          <ChevronDown className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-180")} aria-hidden="true" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          side="bottom"
          align="start"
          sideOffset={6}
          className="z-[90] w-64 rounded-xl border border-line bg-surface p-3 text-ink shadow-lg outline-none"
          data-testid={`state-details-popover-${item.path}`}
        >
          <StateBreakdownList entries={entries} total={total} t={t} />
          <Popover.Arrow className="fill-line" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function StateCell({ item, t }: { item: ArchiveListItem; t: Translate }) {
  const badge = itemStateBadge(item, t);
  if (isDirectory(item) && item.state_counts) {
    return <DirectoryStateControl item={item} t={t} />;
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
        className="inline-flex min-h-11 items-center gap-1 uppercase tracking-wide text-muted outline-none transition-colors duration-150 hover:text-ink hover:underline hover:decoration-green hover:underline-offset-4 focus-visible:text-ink focus-visible:underline focus-visible:decoration-green focus-visible:underline-offset-4 motion-reduce:transition-none"
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
  selectedFilePath = null,
  renderedFilePath = selectedFilePath,
  onCloseFile,
  onFileDetailExited,
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
  const desktopListRef = useRef<HTMLDivElement>(null);
  const [compactDirectoryActions, setCompactDirectoryActions] = useState(false);

  useLayoutEffect(() => {
    const list = desktopListRef.current;
    if (!list || typeof ResizeObserver === "undefined") return;
    const updateLayout = (width: number) => {
      if (width > 0) {
        // At intermediate widths, preserving one stable row is more important
        // than squeezing two primary buttons into the actions cell.
        setCompactDirectoryActions(width < 1040);
      }
    };
    updateLayout(list.getBoundingClientRect().width);
    const observer = new ResizeObserver(([entry]) => {
      updateLayout(entry.contentRect.width);
    });
    observer.observe(list);
    return () => observer.disconnect();
  }, []);

  return (
    <>
      <ul
        data-testid="file-list-cards"
        className="grid gap-2 md:hidden"
      >
        {items.map((item) => {
          const size = itemSizeBytes(item);
          const deep = isDeepArchiveRow(item);
          const jobs = jobsByPath?.get(item.path) ?? [];
          const selected = item.type === "file" && selectedFilePath === item.path;
          const openItem = () => {
            if (isDirectory(item)) onOpenDirectory(item.path);
            else onOpenFile(item.path);
          };
          return (
            <li
              key={`${item.type}:${item.path}`}
              className={cn(
                "group/mobile-card relative isolate flex min-h-[6.25rem] items-start gap-2 overflow-hidden rounded-card border bg-surface p-3 transition-[background-color,border-color,box-shadow] duration-150 motion-reduce:transition-none",
                "hover:border-[var(--interactive-border-hover)] hover:bg-green-soft/25 has-[[aria-expanded=true]]:border-green/60 has-[[aria-expanded=true]]:bg-green-soft/55 has-[[aria-expanded=true]]:ring-2 has-[[aria-expanded=true]]:ring-ring/35",
                deep && "deep-archive-row border-l-4 border-l-[var(--deep-archive-accent)] bg-[var(--deep-archive-row)]",
                selected && "border-green/60 bg-green-soft/55 shadow-[0_5px_18px_var(--interactive-shadow-hover)]",
              )}
              data-path={item.path}
              data-deep-archive={deep ? "true" : undefined}
              data-selected={selected ? "true" : undefined}
              data-testid={`mobile-file-card-${item.path}`}
            >
              <button
                type="button"
                className="absolute inset-0 z-0 rounded-[inherit] outline-none"
                aria-label={item.name}
                aria-expanded={item.type === "file" ? selected : undefined}
                data-directory={isDirectory(item) ? item.path : undefined}
                data-file-path={item.type === "file" ? item.path : undefined}
                data-testid={`mobile-file-card-trigger-${item.path}`}
                onClick={openItem}
              />
              <div className="pointer-events-none relative z-[1] min-w-0 flex-1">
                <div className="group/entry flex min-h-11 items-center gap-2.5 text-left">
                  <ItemNameContent item={item} t={t} />
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {isDirectory(item) && item.state_counts ? (
                    <div className="pointer-events-auto relative z-10 w-full">
                      <DirectoryStateControl item={item} t={t} mobile />
                    </div>
                  ) : (
                    <StateCell item={item} t={t} />
                  )}
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
                  <div className="pointer-events-auto relative z-10 mt-2">
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
                <div className="relative z-10">
                  <MoreActionsButton
                    path={item.path}
                    t={t}
                    onOpenActions={onOpenActions}
                  />
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>

      <div
        ref={desktopListRef}
        data-testid="file-list-table"
        data-desktop-file-list
        className="hidden md:block"
      >
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
              <th className="py-2 pr-3 font-bold">
                <span className="sr-only">{t("ui.more_actions")}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const size = itemSizeBytes(item);
              const deep = isDeepArchiveRow(item);
              const jobs = jobsByPath?.get(item.path) ?? [];
              const selected = item.type === "file" && selectedFilePath === item.path;
              const detailRendered =
                item.type === "file" && renderedFilePath === item.path;
              const directory = isDirectory(item);
              return (
                <Fragment key={`${item.type}:${item.path}`}>
                <tr
                  className={cn(
                    deep
                      ? "deep-archive-row border-b border-line bg-[var(--deep-archive-row)] transition-[background-color,box-shadow] duration-150 has-[[aria-expanded=true]]:shadow-[inset_0_0_0_999px_color-mix(in_srgb,var(--green-soft)_36%,transparent)] last:border-b-0 [&>td:first-child]:shadow-[inset_4px_0_0_0_var(--deep-archive-accent)] motion-reduce:transition-none"
                      : "border-b border-line transition-colors duration-150 hover:bg-green-soft/60 has-[[aria-expanded=true]]:bg-green-soft/60 last:border-b-0 motion-reduce:transition-none",
                    deep &&
                      !selected &&
                      "hover:shadow-[inset_0_0_0_999px_color-mix(in_srgb,var(--green-soft)_36%,transparent)]",
                    selected &&
                      (deep
                        ? "shadow-[inset_0_0_0_999px_color-mix(in_srgb,var(--green-soft)_36%,transparent)]"
                        : "bg-green-soft/70 shadow-[inset_3px_0_0_var(--green)]"),
                  )}
                  data-path={item.path}
                  data-deep-archive={deep ? "true" : undefined}
                  data-selected={selected ? "true" : undefined}
                  data-testid={`desktop-file-row-${item.path}`}
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
                    <div className="flex flex-nowrap items-center gap-2">
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
                  <td className="whitespace-nowrap py-2 pr-3 align-middle">
                    {directory && !jobs.length ? (
                      <DirectoryActions
                        item={item}
                        t={t}
                        capabilities={capabilities}
                        onDesktopAction={onDesktopAction}
                        compact={compactDirectoryActions}
                      />
                    ) : (
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
                    )}
                  </td>
                </tr>
                {detailRendered ? (
                  <tr
                    className="bg-surface"
                    data-testid={`desktop-file-detail-${item.path}`}
                  >
                    <td colSpan={5} className="p-0">
                      <div
                        className="path-history-inline-shell"
                        data-open={selected ? "true" : "false"}
                      >
                        <div className="path-history-inline-clip">
                          <div className="px-3 pb-3 pt-2">
                            <PathHistoryPanel
                              path={item.path}
                              t={t}
                              variant="inline"
                              open={selected}
                              onClose={onCloseFile}
                              onExited={onFileDetailExited}
                            />
                          </div>
                        </div>
                      </div>
                    </td>
                  </tr>
                ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
