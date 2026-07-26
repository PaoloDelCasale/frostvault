import type {
  ArchiveListItem,
  FileOperationAction,
  JobGroup,
  JobStatus,
  MeVault,
} from "@/api/types";

import { isDirectory, isVaultFile } from "./fileLabels";

export type VaultCapabilities = Pick<
  MeVault,
  | "can_operate"
  | "delete_enabled"
  | "cloud_deletion_enabled"
  | "is_vault_owner"
  | "role"
>;

export type RowActionId =
  | "upload"
  | "recover"
  | "free-space"
  | "cloud-archive"
  | "cloud-purge";

export type RowAction = {
  id: RowActionId;
  /** Count of eligible files when acting on a directory; 1 for a file. */
  count: number;
  tone: "danger" | "default";
};

const TERMINAL_JOB_STATUSES = new Set(["completed", "failed", "cancelled"]);

/** Map a row action to the POST path that starts it. */
export function endpointForAction(action: RowActionId): string {
  return `/api/${action}`;
}

export function isActiveJobStatus(status: JobStatus | string): boolean {
  return !TERMINAL_JOB_STATUSES.has(status);
}

export function activeJobsForPath(
  groups: JobGroup[],
  path: string,
): JobGroup[] {
  return groups.filter(
    (group) => group.path === path && isActiveJobStatus(group.status),
  );
}

function fileHasCloudContent(item: ArchiveListItem): boolean {
  if (!isVaultFile(item)) return false;
  return Boolean(
    item.cloud_exists || item.state === "cloud_only" || item.state === "both",
  );
}

/**
 * Actions offered for a Vault File / directory, gated by /api/me capabilities
 * and the same eligibility rules as `app/static/app.js` `renderActions`.
 */
export function availableActions(
  item: ArchiveListItem,
  caps: VaultCapabilities,
): RowAction[] {
  const actions: RowAction[] = [];

  if (caps.can_operate) {
    if (isDirectory(item)) {
      const available = item.available_actions || {};
      if (available.upload) {
        actions.push({
          id: "upload",
          count: available.upload,
          tone: "default",
        });
      }
      if (available.recover) {
        actions.push({
          id: "recover",
          count: available.recover,
          tone: "default",
        });
      }
      if (available["free-space"] && caps.delete_enabled) {
        actions.push({
          id: "free-space",
          count: available["free-space"],
          tone: "danger",
        });
      }
    } else {
      if (item.upload_eligible) {
        actions.push({ id: "upload", count: 1, tone: "default" });
      }
      if (item.recover_eligible) {
        actions.push({ id: "recover", count: 1, tone: "default" });
      }
      if (item.cleanup_eligible && caps.delete_enabled) {
        actions.push({ id: "free-space", count: 1, tone: "danger" });
      }
    }
  }

  if (
    caps.cloud_deletion_enabled &&
    caps.is_vault_owner &&
    isVaultFile(item) &&
    fileHasCloudContent(item)
  ) {
    actions.push({ id: "cloud-archive", count: 1, tone: "default" });
    actions.push({ id: "cloud-purge", count: 1, tone: "danger" });
  }

  return actions;
}

type Translate = (key: string, params?: Record<string, string | number>) => string;

const ROW_ACTION_LABEL_KEYS: Record<RowActionId, string> = {
  upload: "ui.row_action_upload",
  recover: "ui.row_action_recover",
  "free-space": "ui.row_action_free_space",
  "cloud-archive": "ui.row_action_cloud_archive",
  "cloud-purge": "ui.row_action_cloud_purge",
};

export function actionLabel(
  action: RowActionId | FileOperationAction | string,
  t: Translate,
  options?: { count?: number; isDirectory?: boolean },
): string {
  const rowKey = ROW_ACTION_LABEL_KEYS[action as RowActionId];
  let label: string;
  if (rowKey) {
    label = t(rowKey);
  } else {
    const key = `action.${action}`;
    const base = t(key);
    label = base === key ? action : base;
  }
  if (options?.isDirectory && (options.count ?? 1) > 1) {
    return t("ui.action_directory_count", {
      action: label,
      count: options.count ?? 1,
    });
  }
  return label;
}

export function operationStatusLabel(
  operation: { action: string; status: string },
  t: Translate,
): string {
  if (operation.status === "completed") {
    if (operation.action === "upload") return t("operation.upload_verified");
    if (operation.action === "rename") return t("operation.rename_completed");
    return t("operation.completed");
  }
  const statusKey = `operation.${operation.status}`;
  const statusLabel = t(statusKey);
  if (statusLabel !== statusKey) return statusLabel;
  return actionLabel(operation.action, t) || t("operation.generic");
}

/** Destructive actions that require ConfirmDialog before the API call. */
export function isDestructiveAction(action: RowActionId): boolean {
  return (
    action === "free-space" ||
    action === "cloud-archive" ||
    action === "cloud-purge"
  );
}
