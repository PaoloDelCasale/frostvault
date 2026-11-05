import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  ApiError,
  ReauthenticationRedirectError,
  apiQueryKeys,
  approveRecover,
  cancelJob,
  estimateRecover,
  fetchCloudDeletion,
  fetchFileVersions,
  jobsQueryOptions,
  previewCloudDeletion,
  startCloudPurge,
  startFreeSpace,
  startRecover,
  startUpload,
} from "@/api";
import { DEMO_MODE_ENABLED } from "@/demoGate";
import type {
  ArchiveListItem,
  ArchiveVersionItem,
  CloudDeletionPreview,
  CloudDeletionSettings,
  JobGroup,
  RecoverEstimateResponse,
} from "@/api/types";
import { BottomSheet } from "@/components/BottomSheet";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Toast } from "@/components/Toast";

import {
  actionHint,
  actionLabel,
  availableActions,
  endpointForAction,
  groupRowActions,
  ROW_ACTION_CATEGORY_LABEL_KEYS,
  isDestructiveAction,
  type RowActionId,
  type VaultCapabilities,
} from "./actions";
import { CloudPurgeDialog } from "./CloudPurgeDialog";
import { isDirectory } from "./fileLabels";
import { RecoverConfirmDialog } from "./RecoverConfirmDialog";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type FileOperationsHostProps = {
  items: ArchiveListItem[];
  capabilities: VaultCapabilities;
  vaultName: string;
  t: Translate;
  sheetPath: string | null;
  onSheetPathChange: (path: string | null) => void;
  /** Screenshot helper: open a destructive ConfirmDialog immediately. */
  demoConfirm?: { action: string; path: string } | null;
  children: (ctx: {
    jobsByPath: Map<string, JobGroup[]>;
    onOpenActions: (path: string) => void;
    onDesktopAction: (path: string, action: RowActionId) => void;
    onCancelJob: (job: JobGroup) => void;
    onApproveJob: (job: JobGroup) => void;
    cancelBusyId: string | null;
    approveBusyId: string | null;
  }) => ReactNode;
};

type PendingRecover = {
  path: string;
  version: ArchiveVersionItem;
  estimate: RecoverEstimateResponse | null;
  estimateError: string | null;
  defaults: { restore_tier: string; restore_days: number };
};

/**
 * Hosts jobs polling, row-action sheet/dialogs, and mutation dispatch for the
 * archive file list. Children receive job/action callbacks without owning them.
 */
export function FileOperationsHost({
  items,
  capabilities,
  vaultName,
  t,
  sheetPath,
  onSheetPathChange,
  demoConfirm = null,
  children,
}: FileOperationsHostProps) {
  const queryClient = useQueryClient();
  const jobsQuery = useQuery(jobsQueryOptions());
  const [notice, setNotice] = useState<{
    message: string;
    error: boolean;
  } | null>(null);
  const [confirmAction, setConfirmAction] = useState<{
    action: RowActionId;
    path: string;
    isDirectory: boolean;
  } | null>(null);
  const [recover, setRecover] = useState<PendingRecover | null>(null);
  const [purge, setPurge] = useState<{
    path: string;
    isDirectory: boolean;
    settings: CloudDeletionSettings;
    preview: CloudDeletionPreview;
  } | null>(null);
  const [cancelBusyId, setCancelBusyId] = useState<string | null>(null);
  const [approveBusyId, setApproveBusyId] = useState<string | null>(null);
  const itemsByPath = useMemo(() => {
    const map = new Map<string, ArchiveListItem>();
    for (const item of items) map.set(item.path, item);
    return map;
  }, [items]);

  const jobsByPath = useMemo(() => {
    const map = new Map<string, JobGroup[]>();
    for (const group of jobsQuery.data?.groups ?? []) {
      if (["completed", "failed", "cancelled"].includes(group.status)) continue;
      const list = map.get(group.path) ?? [];
      list.push(group);
      map.set(group.path, list);
    }
    return map;
  }, [jobsQuery.data?.groups]);

  const refreshAfterMutation = useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: apiQueryKeys.jobs }),
      queryClient.invalidateQueries({ queryKey: ["files"] }),
      queryClient.invalidateQueries({ queryKey: apiQueryKeys.stats }),
    ]);
  }, [queryClient]);

  const showError = useCallback((error: unknown) => {
    if (error instanceof ReauthenticationRedirectError) return;
    const message =
      error instanceof ApiError
        ? error.message
        : error instanceof Error
          ? error.message
          : String(error);
    setNotice({ message, error: true });
  }, []);

  const showSuccess = useCallback((message: string) => {
    setNotice({ message, error: false });
  }, []);

  const dispatchAction = useCallback(
    async (
      action: RowActionId,
      path: string,
      isDir: boolean,
      extra?: Record<string, unknown>,
    ) => {
      let result: { message?: string };
      switch (action) {
        case "upload":
          result = await startUpload({ path, is_directory: isDir, ...extra });
          break;
        case "recover":
          result = await startRecover({ path, is_directory: isDir, ...extra });
          break;
        case "free-space":
          result = await startFreeSpace({ path, is_directory: isDir, ...extra });
          break;
        case "cloud-purge":
          result = await startCloudPurge({
            path,
            is_directory: isDir,
            confirmation: String(extra?.confirmation ?? ""),
            reason: String(extra?.reason ?? ""),
          });
          break;
        default:
          throw new Error(`Unknown action: ${action}`);
      }
      showSuccess(result.message ?? t("ui.starting"));
      await refreshAfterMutation();
    },
    [refreshAfterMutation, showSuccess, t],
  );

  const beginRecover = useCallback(
    async (path: string, isDir: boolean) => {
      if (isDir) {
        await dispatchAction("recover", path, true);
        return;
      }
      const versions = await fetchFileVersions(path);
      const recoverable = (versions.items || []).filter((item) => item.recoverable);
      if (!recoverable.length) {
        throw new Error(t("ui.recover_no_versions"));
      }
      const defaults = {
        restore_tier: versions.default_restore_tier,
        restore_days: versions.default_restore_days,
      };
      const version = recoverable[0]!;
      let estimate: RecoverEstimateResponse | null = null;
      let estimateError: string | null = null;
      try {
        estimate = await estimateRecover({
          path,
          archive_version_id: version.id,
          restore_tier: defaults.restore_tier,
          restore_days: defaults.restore_days,
        });
      } catch (error) {
        estimateError =
          error instanceof Error
            ? error.message
            : t("ui.recover_estimate_failed");
        if (!estimateError) estimateError = t("ui.recover_estimate_failed");
      }
      setRecover({ path, version, estimate, estimateError, defaults });
    },
    [dispatchAction, t],
  );

  const beginCloudPurge = useCallback(
    async (path: string, isDir: boolean) => {
      const [settings, preview] = await Promise.all([
        fetchCloudDeletion(),
        previewCloudDeletion({ path, is_directory: isDir }),
      ]);
      setPurge({ path, isDirectory: isDir, settings, preview });
    },
    [],
  );

  const startActionFlow = useCallback(
    async (action: RowActionId, path: string) => {
      const item = itemsByPath.get(path);
      if (!item) return;
      const isDir = isDirectory(item);
      onSheetPathChange(null);
      try {
        if (action === "recover") {
          await beginRecover(path, isDir);
          return;
        }
        if (action === "cloud-purge") {
          await beginCloudPurge(path, isDir);
          return;
        }
        if (isDestructiveAction(action)) {
          setConfirmAction({ action, path, isDirectory: isDir });
          return;
        }
        await dispatchAction(action, path, isDir);
      } catch (error) {
        showError(error);
      }
    },
    [
      beginCloudPurge,
      beginRecover,
      dispatchAction,
      itemsByPath,
      onSheetPathChange,
      showError,
    ],
  );

  const sheetItem = sheetPath ? itemsByPath.get(sheetPath) : undefined;
  const sheetGroups = sheetItem
    ? groupRowActions(availableActions(sheetItem, capabilities)).map((group) => ({
        label: t(ROW_ACTION_CATEGORY_LABEL_KEYS[group.category]),
        actions: group.actions.map((action) => ({
          id: action.id,
          label: actionLabel(action.id, t, {
            count: action.count,
            isDirectory: isDirectory(sheetItem),
          }),
          description: actionHint(action.id, t),
          tone: action.tone,
        })),
      }))
    : [];

  const onCancelJob = useCallback(
    async (job: JobGroup) => {
      setCancelBusyId(job.id);
      try {
        const result = await cancelJob({
          group_id: job.id,
          action: String(job.action),
        });
        // BUG-018: RestoreObject cannot be undone — surface the server message,
        // never claim the Glacier restore itself was cancelled.
        let message = result.message;
        if (job.action === "recover") {
          message = `${result.message} ${t("ui.operation_not_cancellable")}`;
        }
        showSuccess(message);
        await refreshAfterMutation();
      } catch (error) {
        showError(error);
      } finally {
        setCancelBusyId(null);
      }
    },
    [refreshAfterMutation, showError, showSuccess, t],
  );

  const onApproveJob = useCallback(
    async (job: JobGroup) => {
      setApproveBusyId(job.id);
      try {
        const result = await approveRecover(job.id);
        showSuccess(result.message ?? t("ui.approve_restore"));
        await refreshAfterMutation();
      } catch (error) {
        showError(error);
      } finally {
        setApproveBusyId(null);
      }
    },
    [refreshAfterMutation, showError, showSuccess, t],
  );

  // Capture-only deep-links: open confirmation dialogs once.
  const demoBootstrapped = useRef(false);
  const gatedDemoConfirm = DEMO_MODE_ENABLED ? demoConfirm : null;
  useEffect(() => {
    if (demoBootstrapped.current) return;
    if (gatedDemoConfirm?.action === "free-space") {
      demoBootstrapped.current = true;
      setConfirmAction({
        action: gatedDemoConfirm.action as RowActionId,
        path: gatedDemoConfirm.path,
        isDirectory: false,
      });
      return;
    }
  }, [gatedDemoConfirm]);

  // Expose endpoint mapping for tests via data attribute on host.
  const hostRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (hostRef.current) {
      hostRef.current.dataset.endpoints = JSON.stringify({
        upload: endpointForAction("upload"),
        recover: endpointForAction("recover"),
        "free-space": endpointForAction("free-space"),
        "cloud-purge": endpointForAction("cloud-purge"),
      });
    }
  }, []);

  return (
    <div ref={hostRef} data-testid="file-operations-host">
      {children({
        jobsByPath,
        onOpenActions: (path) => onSheetPathChange(path),
        onDesktopAction: (path, action) => {
          void startActionFlow(action, path);
        },
        onCancelJob: (job) => {
          void onCancelJob(job);
        },
        onApproveJob: (job) => {
          void onApproveJob(job);
        },
        cancelBusyId,
        approveBusyId,
      })}

      <BottomSheet
        open={Boolean(sheetPath && sheetItem)}
        onOpenChange={(open) => {
          if (!open) onSheetPathChange(null);
        }}
        title={
          sheetItem
            ? t("ui.row_actions_title", { name: sheetItem.name })
            : t("ui.more_actions")
        }
        groups={sheetGroups}
        onAction={(actionId) => {
          if (!sheetPath) return;
          void startActionFlow(actionId as RowActionId, sheetPath);
        }}
      />

      <ConfirmDialog
        open={Boolean(confirmAction)}
        onOpenChange={(open) => {
          if (!open) setConfirmAction(null);
        }}
        title={t("ui.confirm_free_space_title")}
        description={t("ui.confirm_free_space_description", {
          path: confirmAction?.path ?? "",
        })}
        confirmLabel={
          confirmAction
            ? actionLabel(confirmAction.action, t)
            : t("ui.confirm_action")
        }
        cancelLabel={t("ui.cancel")}
        tone="danger"
        onConfirm={() => {
          if (!confirmAction) return;
          const { action, path, isDirectory: isDir } = confirmAction;
          setConfirmAction(null);
          void dispatchAction(action, path, isDir).catch(showError);
        }}
      />

      <RecoverConfirmDialog
        open={Boolean(recover)}
        onOpenChange={(open) => {
          if (!open) setRecover(null);
        }}
        path={recover?.path ?? ""}
        version={recover?.version ?? null}
        estimate={recover?.estimate ?? null}
        estimateError={recover?.estimateError ?? null}
        t={t}
        onConfirm={() => {
          if (!recover?.version) return;
          const version = recover.version;
          const defaults = recover.defaults;
          const estimate = recover.estimate;
          const path = recover.path;
          setRecover(null);
          void dispatchAction("recover", path, false, {
            archive_version_id: version.id,
            restore_tier:
              estimate?.estimate?.tier || defaults.restore_tier,
            restore_days:
              estimate?.estimate?.days || defaults.restore_days,
          }).catch(showError);
        }}
      />

      <CloudPurgeDialog
        open={Boolean(purge)}
        onOpenChange={(open) => {
          if (!open) setPurge(null);
        }}
        path={purge?.path ?? ""}
        isDirectory={purge?.isDirectory ?? false}
        vaultName={vaultName}
        settings={purge?.settings ?? null}
        preview={purge?.preview ?? null}
        t={t}
        onConfirm={(payload) => {
          if (!purge) return;
          const path = purge.path;
          const isDir = purge.isDirectory;
          setPurge(null);
          void dispatchAction("cloud-purge", path, isDir, payload).catch(
            showError,
          );
        }}
      />

      <Toast
        open={Boolean(notice)}
        message={notice?.message ?? ""}
        variant={notice?.error ? "error" : "success"}
        onClose={() => setNotice(null)}
      />
    </div>
  );
}
