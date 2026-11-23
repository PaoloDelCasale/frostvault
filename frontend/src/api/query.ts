import {
  QueryClient,
  QueryClientProvider,
  type QueryClientConfig,
} from "@tanstack/react-query";
import type { ReactNode } from "react";
import { createElement } from "react";

import { fetchI18nCatalog, fetchNotifications } from "./endpoints";
import type { NotificationListStatus } from "./endpoints";

/**
 * Every query key of the app. A key that is a prefix of others (`files`,
 * `jobs`, `general`, `notifications`) invalidates all of them at once.
 */
export const apiQueryKeys = {
  /** The signed-in Owner and the Vaults they can open: read once per sign-in. */
  session: ["session"] as const,
  loginOptions: ["login-options"] as const,
  i18nCatalog: (locale?: string) => ["i18n", "catalog", locale ?? "default"] as const,

  /** Prefix of the installation overview, one entry per page language. */
  general: ["general"] as const,
  overview: (locale: string) => ["general", locale] as const,
  generalVaults: ["general-vaults"] as const,
  billingExport: ["billing-export"] as const,
  uploadActivity: ["upload-activity"] as const,
  recoveryKit: ["recovery-kit"] as const,
  sourceVolumes: ["source-volumes"] as const,
  sourceDirectory: (volumeAlias: string, path: string) => ["source-directory", volumeAlias, path] as const,

  /** Prefix of every open-Problems page. */
  problems: ["problems"] as const,
  problemsPage: (page: number) => ["problems", page] as const,
  /** Prefix shared by every notifications list query (status-scoped children). */
  notifications: ["notifications"] as const,
  notificationsByStatus: (status: NotificationListStatus) =>
    ["notifications", status] as const,

  /** Per-Vault stats, shared by the Vault pages, Overview and catalog events. */
  vaultStats: (vaultId: number) => ["vault-stats", vaultId] as const,
  /** Prefix of every file list, of one Vault or across Vaults. */
  files: ["files"] as const,
  fileList: (vaultId: number, list: { q: string; state: string; directory: string; page: number; global: boolean }, revision: number) =>
    ["files", vaultId, list.q, list.state, list.directory, list.page, list.global, revision] as const,
  /** Prefix of the one file a link focuses in the list. */
  focusedFiles: ["file-detail"] as const,
  focusedFile: (vaultId: number, path: string | null, revision: number) => ["file-detail", vaultId, path, revision] as const,
  /** Prefix of the file sheet: the item, its versions and its pending deletions. */
  fileSheets: ["details"] as const,
  fileSheet: (vaultId: number, path: string, revision: number) => ["details", vaultId, path, revision] as const,
  fileJobHistories: ["file-job-history"] as const,
  fileJobHistory: (vaultId: number, path: string, beforeId: number | undefined, revision: number) =>
    ["file-job-history", vaultId, path, beforeId, revision] as const,
  linkedJobs: ["linked-job"] as const,
  linkedJob: (vaultId: number, jobId: number | undefined, revision: number) => ["linked-job", vaultId, jobId, revision] as const,
  excludedFolders: (vaultId: number, directory: string, revision: number) => ["excluded-folders", vaultId, directory, revision] as const,
  inclusionPreview: (vaultId: number, path: string, isDirectory: boolean) => ["inclusion-preview", vaultId, path, isDirectory] as const,
  /** The policy as a list reads it; `revision` re-reads it after a change made from the list. */
  operationPolicy: (vaultId: number, revision?: number) =>
    revision === undefined ? ["operation-policy", vaultId] as const : ["operation-policy", vaultId, revision] as const,

  /** Prefix of a Vault's job lists. */
  vaultJobs: (vaultId: number) => ["jobs", vaultId] as const,
  activeJobs: (vaultId: number, moves: boolean, beforeId: number | undefined) => ["jobs", vaultId, moves, beforeId] as const,
  bulkUploadAdmission: (vaultId: number | undefined, groupId: string | undefined) => ["bulk-upload-admission", vaultId, groupId] as const,

  lifecycle: (vaultId: number) => ["lifecycle", vaultId] as const,
  vaultMove: (vaultId: number) => ["vault-move", vaultId] as const,
  duplicateCleanup: (vaultId: number) => ["duplicate-cleanup", vaultId] as const,
  mirrorCleanup: (vaultId: number) => ["mirror-cleanup", vaultId] as const,
  minimumStorage: (vaultId: number) => ["minimum-storage", vaultId] as const,
  retentionLock: (vaultId: number) => ["retention-lock", vaultId] as const,
  cloudUsage: (vaultId: number) => ["cloud-usage", vaultId] as const,
  billedCost: (vaultId: number) => ["billed-cost", vaultId] as const,
  stopProtection: (vaultId: number) => ["stop-protection", vaultId] as const,
  recoverPreview: (targets: readonly { vaultId: number; path: string; isDirectory: boolean }[], tier: string) =>
    ["recover-preview", targets, tier] as const,
};

/**
 * What a catalog change of the open Vault makes stale besides its file list
 * and stats: every active query under these prefixes is read again.
 */
export const catalogRefreshedKeys = [
  apiQueryKeys.general,
  apiQueryKeys.fileSheets,
  apiQueryKeys.focusedFiles,
  apiQueryKeys.fileJobHistories,
  apiQueryKeys.linkedJobs,
  apiQueryKeys.problems,
  apiQueryKeys.notifications,
] as const;

/**
 * Cadence of a read a screen makes for itself: once when it mounts and when
 * its key changes, never again by itself (no focus, reconnect, interval or
 * retry). Nothing is kept once the screen is gone, so coming back reads anew
 * and shows the loading state, never an earlier answer.
 */
export const readOnMount = {
  staleTime: 0,
  gcTime: 0,
  retry: false,
  refetchOnWindowFocus: false,
  refetchOnReconnect: false,
  refetchInterval: false,
} as const;

export function createAppQueryClient(
  config: QueryClientConfig = {},
): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        retry: false,
        refetchOnWindowFocus: false,
      },
    },
    ...config,
  });
}

export function ApiQueryProvider({
  client,
  children,
}: {
  client?: QueryClient;
  children: ReactNode;
}) {
  const queryClient = client ?? createAppQueryClient();
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

export function i18nCatalogQueryOptions(locale?: string) {
  return {
    queryKey: apiQueryKeys.i18nCatalog(locale),
    queryFn: () => fetchI18nCatalog(locale),
  };
}

export function notificationsQueryOptions(
  status: NotificationListStatus = "unread",
  limit = 50,
) {
  return {
    queryKey: apiQueryKeys.notificationsByStatus(status),
    queryFn: () => fetchNotifications({ status, limit }),
  };
}
