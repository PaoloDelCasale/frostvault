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
  "delete_enabled" | "cloud_deletion_enabled"
>;

export type RowActionId =
  | "upload"
  | "recover"
  | "free-space"
  | "cloud-purge";

export type RowAction = {
  id: RowActionId;
  /** Count of eligible files when acting on a directory; 1 for a file. */
  count: number;
  tone: "danger" | "default";
};

export type RowActionCategory = "copies" | "deletion";

export const ROW_ACTION_CATEGORY: Record<RowActionId, RowActionCategory> = {
  upload: "copies",
  recover: "copies",
  "free-space": "copies",
  "cloud-purge": "deletion",
};

export const ROW_ACTION_CATEGORY_LABEL_KEYS: Record<RowActionCategory, string> =
  {
    copies: "ui.row_action_category_copies",
    deletion: "ui.row_action_category_deletion",
  };

const CATEGORY_ORDER: RowActionCategory[] = ["copies", "deletion"];

const PRIMARY_ROW_ACTION_IDS = new Set<RowActionId>(["upload", "recover"]);

export function partitionRowActions(actions: RowAction[]): {
  primary: RowAction[];
  overflow: RowAction[];
} {
  const primary: RowAction[] = [];
  const overflow: RowAction[] = [];
  for (const action of actions) {
    if (PRIMARY_ROW_ACTION_IDS.has(action.id)) primary.push(action);
    else overflow.push(action);
  }
  return { primary, overflow };
}

export function groupRowActions(
  actions: RowAction[],
): Array<{ category: RowActionCategory; actions: RowAction[] }> {
  const buckets = new Map<RowActionCategory, RowAction[]>();
  for (const action of actions) {
    const category = ROW_ACTION_CATEGORY[action.id];
    const list = buckets.get(category) ?? [];
    list.push(action);
    buckets.set(category, list);
  }
  return CATEGORY_ORDER.filter((category) => (buckets.get(category)?.length ?? 0) > 0).map(
    (category) => ({ category, actions: buckets.get(category)! }),
  );
}

const TERMINAL_JOB_STATUSES = new Set(["completed", "failed", "cancelled"]);

/** Map a row action to the POST/PUT path that starts it. */
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
    item.cloud_exists && !item.local_exists && item.state === "cloud_only",
  );
}

function directoryCloudDeletionCount(item: ArchiveListItem): number {
  if (!isDirectory(item) || item.state !== "cloud_only") return 0;
  const available = item.available_actions || {};
  const counted = available["cloud-purge"];
  if (typeof counted === "number" && counted === item.item_count && counted > 0) return counted;
  return 0;
}

/**
 * Actions offered for a Vault File / directory, gated by /api/me capabilities
 * and the same eligibility rules as the legacy archive action renderer.
 */
export function availableActions(
  item: ArchiveListItem,
  caps: VaultCapabilities,
): RowAction[] {
  const actions: RowAction[] = [];

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
      // Not danger: Local Copy only; cloud Archive Versions stay recoverable.
      actions.push({
        id: "free-space",
        count: available["free-space"],
        tone: "default",
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
      actions.push({ id: "free-space", count: 1, tone: "default" });
    }
  }

  if (caps.cloud_deletion_enabled) {
    if (isVaultFile(item) && fileHasCloudContent(item)) {
      actions.push({ id: "cloud-purge", count: 1, tone: "danger" });
    } else if (isDirectory(item)) {
      const cloudCount = directoryCloudDeletionCount(item);
      if (cloudCount > 0) {
        actions.push({ id: "cloud-purge", count: cloudCount, tone: "danger" });
      }
    }
  }

  return actions;
}

type Translate = (key: string, params?: Record<string, string | number>) => string;

const ROW_ACTION_LABEL_KEYS: Record<RowActionId, string> = {
  upload: "ui.row_action_upload",
  recover: "ui.row_action_recover",
  "free-space": "ui.row_action_free_space",
  "cloud-purge": "ui.row_action_cloud_purge",
};

const ROW_ACTION_HINT_KEYS: Record<RowActionId, string> = {
  upload: "ui.row_action_upload_hint",
  recover: "ui.row_action_recover_hint",
  "free-space": "ui.row_action_free_space_hint",
  "cloud-purge": "ui.row_action_cloud_purge_hint",
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

/** Short scope hint shown under each row action in the mobile sheet. */
export function actionHint(
  action: RowActionId | string,
  t: Translate,
): string | undefined {
  const key = ROW_ACTION_HINT_KEYS[action as RowActionId];
  if (!key) return undefined;
  const hint = t(key);
  return hint === key ? undefined : hint;
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
    action === "cloud-purge"
  );
}
