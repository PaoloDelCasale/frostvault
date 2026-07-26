import { Badge } from "@/components/Badge";
import { StorageBadge } from "@/components/StorageBadge";
import { Button } from "@/components/ui/button";
import type { ArchiveListItem, JobGroup } from "@/api/types";

import {
  actionLabel,
  availableActions,
  type RowActionId,
  type VaultCapabilities,
} from "./actions";
import { formatBytes, formatCount } from "./format";
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
  /** Mobile: opens the bottom sheet of row actions. */
  onOpenActions?: (path: string) => void;
  /** Desktop: run an action inline. */
  onDesktopAction?: (path: string, action: RowActionId) => void;
  jobsByPath?: Map<string, JobGroup[]>;
  onCancelJob?: (job: JobGroup) => void;
  onApproveJob?: (job: JobGroup) => void;
  cancelBusyId?: string | null;
  approveBusyId?: string | null;
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
      data-testid={`more-actions-${path}`}
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
  return (
    <div
      className="row-actions compact flex flex-wrap gap-1"
      data-testid={`desktop-actions-${item.path}`}
    >
      {actions.map((action) => (
        <Button
          key={action.id}
          type="button"
          variant={action.tone === "danger" ? "danger" : "secondary"}
          className="min-h-11 min-w-11 px-3"
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
      ))}
    </div>
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

function RowJobOrActions({
  item,
  t,
  capabilities,
  jobs,
  onOpenActions,
  onDesktopAction,
  onCancelJob,
  onApproveJob,
  cancelBusyId,
  approveBusyId,
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
  cancelBusyId?: string | null;
  approveBusyId?: string | null;
  layout: "card" | "table";
}) {
  if (jobs.length) {
    return (
      <div className="progress-stack flex min-w-[190px] flex-col gap-2">
        {jobs.map((job) => (
          <JobProgress
            key={job.id}
            job={job}
            t={t}
            canCancel={capabilities.can_operate}
            canApprove={capabilities.is_vault_owner}
            onCancel={(j) => onCancelJob?.(j)}
            onApprove={(j) => onApproveJob?.(j)}
            cancelBusy={cancelBusyId === job.id}
            approveBusy={approveBusyId === job.id}
          />
        ))}
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
export function FileList({
  items,
  t,
  capabilities,
  onOpenDirectory,
  onOpenFile,
  onOpenActions,
  onDesktopAction,
  jobsByPath,
  onCancelJob,
  onApproveJob,
  cancelBusyId,
  approveBusyId,
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
                  ? "deep-archive-row flex items-start gap-2 border-l-4 border-[#8062ad] bg-[#fcfaff] py-3 pl-2 first:pt-0 last:pb-0"
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
                      cancelBusyId={cancelBusyId}
                      approveBusyId={approveBusyId}
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
              const deep = isDeepArchiveRow(item);
              const jobs = jobsByPath?.get(item.path) ?? [];
              return (
                <tr
                  key={`${item.type}:${item.path}`}
                  className={
                    deep
                      ? "deep-archive-row border-b border-line bg-[#fcfaff] last:border-b-0 [&>td:first-child]:shadow-[inset_4px_0_#8062ad]"
                      : "border-b border-line last:border-b-0"
                  }
                  data-path={item.path}
                  data-deep-archive={deep ? "true" : undefined}
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
                      cancelBusyId={cancelBusyId}
                      approveBusyId={approveBusyId}
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
