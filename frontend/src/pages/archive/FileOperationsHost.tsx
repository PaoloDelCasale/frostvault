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
  accelerateCloudPurge,
  apiQueryKeys,
  approveRecover,
  cancelJob,
  estimateRecover,
  fetchCloudDeletion,
  fetchFileVersions,
  fetchStorageClasses,
  jobsQueryOptions,
  previewCloudDeletion,
  startCloudArchive,
  startCloudPurge,
  startFreeSpace,
  startRecover,
  startStorageClass,
  startUpload,
  updateLifecyclePin,
} from "@/api";
import type {
  ArchiveListItem,
  ArchiveVersionItem,
  CloudDeletionPreview,
  CloudDeletionSettings,
  JobGroup,
  RecoverEstimateResponse,
} from "@/api/types";
import { BottomSheet, type BottomSheetAction } from "@/components/BottomSheet";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Toast } from "@/components/Toast";
import { ensurePushSubscription } from "@/pwa";

import {
  actionHint,
  actionLabel,
  availableActions,
  endpointForAction,
  isDestructiveAction,
  type ManualStorageClass,
  type RowActionId,
  type VaultCapabilities,
} from "./actions";
import { CloudPurgeDialog } from "./CloudPurgeDialog";
import { isDirectory } from "./fileLabels";
import { RecoverConfirmDialog } from "./RecoverConfirmDialog";
import { StorageClassDialog } from "./StorageClassDialog";
import {
  sourceNeedsRestoreForClassChange,
  type StorageClassOption,
} from "./storageClassOptions";
import { VersionSelectDialog } from "./VersionSelectDialog";

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
  /** Screenshot helper: open the Archive Version picker for this path. */
  demoVersionsPath?: string | null;
  children: (ctx: {
    jobsByPath: Map<string, JobGroup[]>;
    onOpenActions: (path: string) => void;
    onDesktopAction: (path: string, action: RowActionId) => void;
    onCancelJob: (job: JobGroup) => void;
    onApproveJob: (job: JobGroup) => void;
    onAcceleratePurge: (job: JobGroup) => void;
    cancelBusyId: string | null;
    approveBusyId: string | null;
    accelerateBusyId: string | null;
  }) => ReactNode;
};

type PendingRecover = {
  path: string;
  isDirectory: boolean;
  version: ArchiveVersionItem | null;
  versions: ArchiveVersionItem[];
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
  demoVersionsPath = null,
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
    explanation?: string;
  } | null>(null);
  const [recover, setRecover] = useState<PendingRecover | null>(null);
  const [recoverStep, setRecoverStep] = useState<
    "versions" | "confirm" | null
  >(null);
  const [purge, setPurge] = useState<{
    path: string;
    isDirectory: boolean;
    settings: CloudDeletionSettings;
    preview: CloudDeletionPreview;
  } | null>(null);
  const [storageClassTarget, setStorageClassTarget] = useState<{
    path: string;
    isDirectory: boolean;
    wholeVault?: boolean;
    count: number;
    totalBytes: number;
    currentClass?: string | null;
    restoreState?: string | null;
  } | null>(null);
  const storageClassesQuery = useQuery({
    queryKey: ["storage-classes"],
    queryFn: fetchStorageClasses,
    enabled: Boolean(storageClassTarget),
    staleTime: 60_000,
  });
  const classOptions: StorageClassOption[] = useMemo(
    () =>
      (storageClassesQuery.data?.items ?? []).map((item) => ({
        ...item,
      })),
    [storageClassesQuery.data?.items],
  );
  const restoreEstimate = useMemo(() => {
    if (!storageClassTarget) return null;
    if (
      !sourceNeedsRestoreForClassChange({
        currentClass: storageClassTarget.currentClass,
        restoreState: storageClassTarget.restoreState,
      })
    ) {
      return null;
    }
    const current = (storageClassTarget.currentClass || "").toUpperCase();
    const option = classOptions.find((item) => item.id === current);
    if (!option) return null;
    const gib = storageClassTarget.totalBytes / 1024 ** 3;
    return {
      hours: option.restore_hours_bulk ?? 0,
      costEur: gib * (option.restore_rate_eur_per_gib_bulk ?? 0),
    };
  }, [storageClassTarget, classOptions]);
  const [pinAction, setPinAction] = useState<{
    path: string;
    isDirectory: boolean;
    pinned: boolean;
  } | null>(null);
  const [cancelBusyId, setCancelBusyId] = useState<string | null>(null);
  const [approveBusyId, setApproveBusyId] = useState<string | null>(null);
  const [accelerateBusyId, setAccelerateBusyId] = useState<string | null>(null);
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
      // Long-running Jobs: request push at this moment, never on first load.
      if (
        action === "upload" ||
        action === "recover" ||
        action === "free-space" ||
        action === "storage-class" ||
        action === "cloud-archive" ||
        action === "cloud-purge"
      ) {
        void ensurePushSubscription();
      }
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
        case "storage-class":
          result = await startStorageClass({
            path,
            is_directory: isDir,
            whole_vault: Boolean(extra?.whole_vault),
            target_storage_class: String(extra?.target_storage_class ?? ""),
            archive_version_id:
              typeof extra?.archive_version_id === "string"
                ? extra.archive_version_id
                : undefined,
            pin_after: Boolean(extra?.pin_after),
          });
          break;
        case "lifecycle-pin":
        case "lifecycle-unpin":
          result = await updateLifecyclePin({
            path,
            is_directory: isDir,
            pinned: action === "lifecycle-pin",
          });
          break;
        case "cloud-archive":
          result = await startCloudArchive({ path, is_directory: isDir, ...extra });
          break;
        case "cloud-purge":
          result = await startCloudPurge({
            path,
            is_directory: isDir,
            confirmation: String(extra?.confirmation ?? ""),
            reason: String(extra?.reason ?? ""),
            generated_phrase: String(extra?.generated_phrase ?? ""),
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
      if (recoverable.length === 1) {
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
        setRecover({
          path,
          isDirectory: false,
          version,
          versions: recoverable,
          estimate,
          estimateError,
          defaults,
        });
        setRecoverStep("confirm");
        return;
      }
      setRecover({
        path,
        isDirectory: false,
        version: null,
        versions: recoverable,
        estimate: null,
        estimateError: null,
        defaults,
      });
      setRecoverStep("versions");
    },
    [dispatchAction, t],
  );

  const onSelectVersion = useCallback(
    async (version: ArchiveVersionItem) => {
      if (!recover) return;
      let estimate: RecoverEstimateResponse | null = null;
      let estimateError: string | null = null;
      try {
        estimate = await estimateRecover({
          path: recover.path,
          archive_version_id: version.id,
          restore_tier: recover.defaults.restore_tier,
          restore_days: recover.defaults.restore_days,
        });
      } catch (error) {
        estimateError =
          error instanceof Error
            ? error.message
            : t("ui.recover_estimate_failed");
      }
      setRecover({
        ...recover,
        version,
        estimate,
        estimateError: estimateError || (estimate ? null : t("ui.recover_estimate_failed")),
      });
      setRecoverStep("confirm");
    },
    [recover, t],
  );

  const beginCloudArchive = useCallback(
    async (path: string, isDir: boolean) => {
      const settings = await fetchCloudDeletion();
      setConfirmAction({
        action: "cloud-archive",
        path,
        isDirectory: isDir,
        explanation: settings.delete_marker_explanation,
      });
    },
    [],
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
        if (action === "cloud-archive") {
          await beginCloudArchive(path, isDir);
          return;
        }
        if (action === "cloud-purge") {
          await beginCloudPurge(path, isDir);
          return;
        }
        if (action === "storage-class") {
          const actions = availableActions(item, capabilities);
          const storageAction = actions.find((entry) => entry.id === "storage-class");
          setStorageClassTarget({
            path,
            isDirectory: isDir,
            count: storageAction?.count ?? 1,
            totalBytes: Number(
              isDir
                ? item.cloud_size || item.total_size || 0
                : item.cloud_size || item.local_size || 0,
            ),
            currentClass: item.storage_class,
            restoreState: isDir ? null : item.restore_state,
          });
          return;
        }
        if (action === "lifecycle-pin" || action === "lifecycle-unpin") {
          setPinAction({
            path,
            isDirectory: isDir,
            pinned: action === "lifecycle-pin",
          });
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
      beginCloudArchive,
      beginCloudPurge,
      beginRecover,
      capabilities,
      dispatchAction,
      itemsByPath,
      onSheetPathChange,
      showError,
    ],
  );

  const sheetItem = sheetPath ? itemsByPath.get(sheetPath) : undefined;
  const sheetActions: BottomSheetAction[] = sheetItem
    ? availableActions(sheetItem, capabilities).map((action) => ({
        id: action.id,
        label: actionLabel(action.id, t, {
          count: action.count,
          isDirectory: isDirectory(sheetItem),
        }),
        description: actionHint(action.id, t),
        tone: action.tone,
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

  const onAcceleratePurge = useCallback(
    async (job: JobGroup) => {
      setAccelerateBusyId(job.id);
      try {
        const result = await accelerateCloudPurge(job.id);
        showSuccess(result.message ?? t("ui.purge_now"));
        await refreshAfterMutation();
      } catch (error) {
        showError(error);
      } finally {
        setAccelerateBusyId(null);
      }
    },
    [refreshAfterMutation, showError, showSuccess, t],
  );

  // Screenshot / demo deep-links: open confirm or version dialogs once.
  const demoBootstrapped = useRef(false);
  useEffect(() => {
    if (demoBootstrapped.current) return;
    if (demoConfirm?.action === "free-space" || demoConfirm?.action === "cloud-archive") {
      demoBootstrapped.current = true;
      setConfirmAction({
        action: demoConfirm.action as RowActionId,
        path: demoConfirm.path,
        isDirectory: false,
        explanation:
          demoConfirm.action === "cloud-archive"
            ? "Delete markers hide the current key."
            : undefined,
      });
      return;
    }
    if (demoConfirm?.action === "storage-class") {
      demoBootstrapped.current = true;
      setStorageClassTarget({
        path: demoConfirm.path,
        isDirectory: false,
        count: 1,
        totalBytes: 2048,
        currentClass: "STANDARD",
      });
      return;
    }
    if (
      demoConfirm?.action === "lifecycle-pin" ||
      demoConfirm?.action === "lifecycle-unpin"
    ) {
      demoBootstrapped.current = true;
      setPinAction({
        path: demoConfirm.path,
        isDirectory: false,
        pinned: demoConfirm.action === "lifecycle-pin",
      });
      return;
    }
    if (demoVersionsPath) {
      demoBootstrapped.current = true;
      void beginRecover(demoVersionsPath, false).catch(showError);
    }
  }, [beginRecover, demoConfirm, demoVersionsPath, showError]);

  // Expose endpoint mapping for tests via data attribute on host.
  const hostRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (hostRef.current) {
      hostRef.current.dataset.endpoints = JSON.stringify({
        upload: endpointForAction("upload"),
        recover: endpointForAction("recover"),
        "free-space": endpointForAction("free-space"),
        "storage-class": endpointForAction("storage-class"),
        "lifecycle-pin": endpointForAction("lifecycle-pin"),
        "cloud-archive": endpointForAction("cloud-archive"),
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
        onAcceleratePurge: (job) => {
          void onAcceleratePurge(job);
        },
        cancelBusyId,
        approveBusyId,
        accelerateBusyId,
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
        actions={sheetActions}
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
        title={
          confirmAction?.action === "cloud-archive"
            ? t("ui.confirm_cloud_archive_title")
            : t("ui.confirm_free_space_title")
        }
        description={
          confirmAction?.action === "cloud-archive"
            ? t("ui.confirm_cloud_archive_description", {
                path: confirmAction.path,
                explanation: confirmAction.explanation ?? "",
              })
            : t("ui.confirm_free_space_description", {
                path: confirmAction?.path ?? "",
              })
        }
        confirmLabel={
          confirmAction
            ? actionLabel(confirmAction.action, t)
            : t("ui.confirm_action")
        }
        cancelLabel={t("ui.cancel")}
        tone={
          confirmAction?.action === "cloud-archive" ? "default" : "danger"
        }
        onConfirm={() => {
          if (!confirmAction) return;
          const { action, path, isDirectory: isDir } = confirmAction;
          setConfirmAction(null);
          void dispatchAction(action, path, isDir).catch(showError);
        }}
      />

      <StorageClassDialog
        open={Boolean(storageClassTarget)}
        onOpenChange={(open) => {
          if (!open) setStorageClassTarget(null);
        }}
        path={storageClassTarget?.path ?? ""}
        count={storageClassTarget?.count ?? 1}
        totalBytes={storageClassTarget?.totalBytes ?? 0}
        currentClass={storageClassTarget?.currentClass}
        restoreState={storageClassTarget?.restoreState}
        classOptions={classOptions}
        restoreEstimate={restoreEstimate}
        t={t}
        onConfirm={(target: ManualStorageClass, options) => {
          if (!storageClassTarget) return;
          const pending = storageClassTarget;
          setStorageClassTarget(null);
          void dispatchAction("storage-class", pending.path, pending.isDirectory, {
            target_storage_class: target,
            whole_vault: pending.wholeVault,
            pin_after: options.pinAfter,
          }).catch(showError);
        }}
      />

      <ConfirmDialog
        open={Boolean(pinAction)}
        onOpenChange={(open) => {
          if (!open) setPinAction(null);
        }}
        title={
          pinAction?.pinned
            ? t("ui.lifecycle_pin_confirm_title")
            : t("ui.lifecycle_unpin_confirm_title")
        }
        description={
          pinAction?.pinned
            ? t("ui.lifecycle_pin_confirm_body")
            : t("ui.lifecycle_unpin_confirm_body")
        }
        confirmLabel={
          pinAction?.pinned
            ? t("ui.row_action_lifecycle_pin")
            : t("ui.row_action_lifecycle_unpin")
        }
        cancelLabel={t("ui.cancel")}
        tone="default"
        onConfirm={() => {
          if (!pinAction) return;
          const pending = pinAction;
          setPinAction(null);
          void dispatchAction(
            pending.pinned ? "lifecycle-pin" : "lifecycle-unpin",
            pending.path,
            pending.isDirectory,
          ).catch(showError);
        }}
      />

      <VersionSelectDialog
        open={recoverStep === "versions" && Boolean(recover)}
        onOpenChange={(open) => {
          if (!open) {
            setRecover(null);
            setRecoverStep(null);
          }
        }}
        path={recover?.path ?? ""}
        versions={recover?.versions ?? []}
        t={t}
        onSelect={(version) => {
          void onSelectVersion(version);
        }}
      />

      <RecoverConfirmDialog
        open={recoverStep === "confirm" && Boolean(recover?.version)}
        onOpenChange={(open) => {
          if (!open) {
            setRecover(null);
            setRecoverStep(null);
          }
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
          setRecoverStep(null);
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
