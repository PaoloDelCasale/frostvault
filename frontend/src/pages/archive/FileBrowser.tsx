import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { DEMO_MODE_ENABLED, getDemoSearchParam } from "@/demoGate";
import {
  DEFAULT_PAGE_SIZE,
  countActiveJobGroups,
  filesQueryOptions,
  filesRefetchIntervalFromJobs,
  jobsQueryOptions,
} from "@/api";
import type { FilesResponse } from "@/api/types";
import { MenuSelect } from "@/components/MenuSelect";
import { Button } from "@/components/ui/button";
import {
  isBrowserOffline,
  isOfflineCacheContext,
  loadCachedFilesListing,
  offlineFileCacheRequestHeaders,
  saveCachedFilesListing,
  type OfflineCacheContext,
  type OfflineFileCacheLease,
} from "@/pwa/offlineFiles";

import type { VaultCapabilities } from "./actions";
import { demoRootListing } from "./demoFiles";
import { FileList } from "./FileList";
import { FileOperationsHost } from "./FileOperationsHost";
import {
  buildBreadcrumbs,
  collapseBreadcrumbs,
  isBreadcrumbEllipsis,
  parentDirectory,
} from "./fileLabels";
import {
  parseFileSortKey,
  parseFileSortOrder,
  type FileSortKey,
  type FileSortOrder,
} from "./fileSort";
import { PathHistoryPanel } from "./PathHistoryPanel";
import { RenameCandidatesPanel } from "./RenameCandidatesPanel";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type FileBrowserProps = {
  t: Translate;
  capabilities: VaultCapabilities;
  /** Authenticated User identity. Listings are not persisted without it. */
  userId?: number;
  /** Current Vault identity, used to isolate file and candidate query caches. */
  vaultId: number;
  /** Server-issued Session/Vault generation; App supplies it even network-only. */
  authorizationGeneration?: string;
  /**
   * App passes a lease or explicit null. Undefined is retained for isolated
   * component consumers that exercise the serialization seam without a Worker.
   */
  offlineCacheLease?: OfflineFileCacheLease | null;
  vaultName: string;
};

export const SEARCH_DEBOUNCE_MS = 250;

function readSearchParams(): {
  directory: string;
  q: string;
  state: string;
  storage: string;
  page: number;
  sort: FileSortKey;
  order: FileSortOrder;
} {
  const params = new URLSearchParams(window.location.search);
  const pageRaw = Number(params.get("page") || "1");
  return {
    directory: params.get("directory") || "",
    q: params.get("q") || "",
    state: params.get("state") || "",
    storage: params.get("storage") || "",
    page: Number.isFinite(pageRaw) && pageRaw >= 1 ? pageRaw : 1,
    sort: parseFileSortKey(params.get("sort")),
    order: parseFileSortOrder(params.get("order")),
  };
}

function writeSearchParams(
  next: {
    directory: string;
    q: string;
    state: string;
    storage: string;
    page: number;
    sort: FileSortKey;
    order: FileSortOrder;
  },
  mode: "push" | "replace",
) {
  const url = new URL(window.location.href);
  if (next.directory) url.searchParams.set("directory", next.directory);
  else url.searchParams.delete("directory");
  if (next.q) url.searchParams.set("q", next.q);
  else url.searchParams.delete("q");
  if (next.state) url.searchParams.set("state", next.state);
  else url.searchParams.delete("state");
  if (next.storage) url.searchParams.set("storage", next.storage);
  else url.searchParams.delete("storage");
  if (next.page > 1) url.searchParams.set("page", String(next.page));
  else url.searchParams.delete("page");
  if (next.sort !== "name") url.searchParams.set("sort", next.sort);
  else url.searchParams.delete("sort");
  if (next.order !== "asc") url.searchParams.set("order", next.order);
  else url.searchParams.delete("order");
  const href = `${url.pathname}${url.search}${url.hash}`;
  if (mode === "push") {
    window.history.pushState({ ...next }, "", href);
  } else {
    window.history.replaceState({ ...next }, "", href);
  }
}

/**
 * Responsive archive listing: sticky search/breadcrumbs, cards below md,
 * table from md up, Path History on file tap.
 */
export function FileBrowser({
  t,
  capabilities,
  userId,
  vaultId,
  authorizationGeneration,
  offlineCacheLease,
  vaultName,
}: FileBrowserProps) {
  const initial = readSearchParams();
  const [directory, setDirectory] = useState(initial.directory);
  const [qInput, setQInput] = useState(initial.q);
  const [q, setQ] = useState(initial.q);
  const [state, setState] = useState(initial.state);
  const [storage, setStorage] = useState(initial.storage);
  const [page, setPage] = useState(initial.page);
  const [sortKey, setSortKey] = useState<FileSortKey>(initial.sort);
  const [sortOrder, setSortOrder] = useState<FileSortOrder>(initial.order);
  const [sheetPath, setSheetPath] = useState<string | null>(() => {
    // Capture helper: ?sheet=<path> opens the actions bottom sheet.
    return getDemoSearchParam("sheet");
  });
  const [historyPath, setHistoryPath] = useState<string | null>(() => {
    // Capture / deep-link helper: ?history=<path> opens Path History.
    return getDemoSearchParam("history");
  });
  const demoConfirm = getDemoSearchParam("confirm");
  const demoConfirmTarget = getDemoSearchParam("target") || "readme.txt";
  const demoVersions = getDemoSearchParam("versions");

  const query = useMemo(
    () => ({
      q,
      state,
      storage,
      directory,
      page,
      page_size: DEFAULT_PAGE_SIZE,
      sort: sortKey,
      order: sortOrder,
    }),
    [q, state, storage, directory, page, sortKey, sortOrder],
  );
  const offlineCacheContext = useMemo<OfflineCacheContext | null>(() => {
    const context = {
      userId,
      vaultId,
      authorizationGeneration:
        offlineCacheLease?.context.authorizationGeneration ?? authorizationGeneration,
    };
    return isOfflineCacheContext(context) ? context : null;
  }, [authorizationGeneration, offlineCacheLease, userId, vaultId]);
  const fileQueryKey = [
    "files",
    offlineCacheContext?.userId ?? "no-user",
    offlineCacheContext?.vaultId ?? "no-vault",
    offlineCacheContext?.authorizationGeneration ?? "no-authorization",
    query.q ?? "",
    query.state ?? "",
    typeof query.storage === "string" ? query.storage : "",
    query.directory ?? "",
    query.page ?? 1,
    query.page_size ?? DEFAULT_PAGE_SIZE,
    query.sort ?? "name",
    query.order ?? "asc",
  ] as const;
  const offlineCacheHeaders = offlineFileCacheRequestHeaders(
    offlineCacheLease,
    offlineCacheContext,
  );
  const offlineCacheRequestOptions = useMemo<RequestInit | undefined>(
    () => (offlineCacheHeaders ? { headers: offlineCacheHeaders } : undefined),
    [offlineCacheHeaders],
  );
  // The undefined branch is an isolated component-test seam. The mounted App
  // always supplies null or a verified lease, so production persistence and
  // Worker caching remain fail-closed.
  const canUseOfflineCache =
    offlineCacheLease === undefined
      ? Boolean(offlineCacheContext)
      : Boolean(offlineCacheHeaders);

  const queryClient = useQueryClient();
  // Share the jobs cache with FileOperationsHost so the list can poll while
  // Jobs are active and refresh as soon as active count drops (issue #128).
  const jobsQuery = useQuery(jobsQueryOptions());
  const activeJobCount = countActiveJobGroups(jobsQuery.data);
  const prevActiveJobCount = useRef<number | null>(null);
  useEffect(() => {
    const prev = prevActiveJobCount.current;
    prevActiveJobCount.current = activeJobCount;
    if (prev !== null && activeJobCount < prev) {
      void queryClient.invalidateQueries({ queryKey: ["files"] });
    }
  }, [activeJobCount, queryClient]);

  const filesQuery = useQuery({
    ...filesQueryOptions(query, offlineCacheRequestOptions),
    queryKey: fileQueryKey,
    refetchInterval: (queryState) => {
      // Bounded loading convergence while the durable projection is still
      // building after migration/restart. Not idle catalog polling: stops as
      // soon as aggregate_status leaves "loading". Catalog events also invalidate.
      if (queryState.state.data?.aggregate_status === "loading") {
        return 1_500;
      }
      return filesRefetchIntervalFromJobs(jobsQuery.data);
    },
    // Keep the previous directory page visible during invalidation/refetch so
    // event-driven updates never flash a false-empty "0 items" table.
    placeholderData: keepPreviousData,
  });
  const forceOfflineDemo =
    DEMO_MODE_ENABLED && getDemoSearchParam("offline") === "1";

  useEffect(() => {
    if (forceOfflineDemo && offlineCacheContext && canUseOfflineCache) {
      saveCachedFilesListing(
        offlineCacheContext,
        query,
        demoRootListing,
        undefined,
        offlineCacheLease ?? undefined,
      );
    }
  }, [
    forceOfflineDemo,
    offlineCacheContext,
    canUseOfflineCache,
    offlineCacheLease,
    query,
  ]);

  useEffect(() => {
    if (
      filesQuery.isSuccess &&
      filesQuery.data &&
      offlineCacheContext &&
      canUseOfflineCache
    ) {
      saveCachedFilesListing(
        offlineCacheContext,
        query,
        filesQuery.data,
        undefined,
        offlineCacheLease ?? undefined,
      );
    }
  }, [
    filesQuery.isSuccess,
    filesQuery.data,
    offlineCacheContext,
    canUseOfflineCache,
    offlineCacheLease,
    query,
  ]);

  const offlineCached = useMemo(() => {
    if (!offlineCacheContext || !canUseOfflineCache) return null;
    if (forceOfflineDemo) {
      return loadCachedFilesListing(
        offlineCacheContext,
        query,
        undefined,
        offlineCacheLease ?? undefined,
      ) ?? {
        data: demoRootListing,
        savedAt: new Date().toISOString(),
      };
    }
    if (filesQuery.isSuccess) return null;
    if (!(filesQuery.isError || isBrowserOffline())) return null;
    return loadCachedFilesListing(
      offlineCacheContext,
      query,
      undefined,
      offlineCacheLease ?? undefined,
    );
  }, [
    filesQuery.isSuccess,
    filesQuery.isError,
    offlineCacheContext,
    canUseOfflineCache,
    offlineCacheLease,
    query,
    forceOfflineDemo,
  ]);

  const displayData: FilesResponse | undefined = forceOfflineDemo
    ? offlineCached?.data
    : (filesQuery.data ?? offlineCached?.data);
  const showingStale = Boolean(
    forceOfflineDemo || (offlineCached && !filesQuery.data),
  );

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (qInput === q) return;
      setQ(qInput);
      setPage(1);
      writeSearchParams(
        {
          directory,
          q: qInput,
          state,
          storage,
          page: 1,
          sort: sortKey,
          order: sortOrder,
        },
        "replace",
      );
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [qInput, q, directory, state, storage, sortKey, sortOrder]);

  useEffect(() => {
    const onPopState = () => {
      const next = readSearchParams();
      setDirectory(next.directory);
      setQInput(next.q);
      setQ(next.q);
      setState(next.state);
      setStorage(next.storage);
      setPage(next.page);
      setSortKey(next.sort);
      setSortOrder(next.order);
      setHistoryPath(null);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  function navigateDirectory(
    path: string,
    historyMode: "push" | "replace" = "push",
  ) {
    setDirectory(path);
    setQInput("");
    setQ("");
    setState("");
    setStorage("");
    setPage(1);
    setHistoryPath(null);
    writeSearchParams(
      {
        directory: path,
        q: "",
        state: "",
        storage: "",
        page: 1,
        sort: sortKey,
        order: sortOrder,
      },
      historyMode,
    );
  }

  function changeState(nextState: string) {
    setState(nextState);
    setPage(1);
    writeSearchParams(
      {
        directory,
        q,
        state: nextState,
        storage,
        page: 1,
        sort: sortKey,
        order: sortOrder,
      },
      "replace",
    );
  }

  function changeStorage(nextStorage: string) {
    setStorage(nextStorage);
    setPage(1);
    writeSearchParams(
      {
        directory,
        q,
        state,
        storage: nextStorage,
        page: 1,
        sort: sortKey,
        order: sortOrder,
      },
      "replace",
    );
  }

  function changePage(nextPage: number) {
    setPage(nextPage);
    writeSearchParams(
      {
        directory,
        q,
        state,
        storage,
        page: nextPage,
        sort: sortKey,
        order: sortOrder,
      },
      "replace",
    );
  }

  function changeSort(nextKey: FileSortKey) {
    const nextOrder: FileSortOrder =
      sortKey === nextKey && sortOrder === "asc" ? "desc" : "asc";
    setSortKey(nextKey);
    setSortOrder(nextOrder);
    setPage(1);
    writeSearchParams(
      {
        directory,
        q,
        state,
        storage,
        page: 1,
        sort: nextKey,
        order: nextOrder,
      },
      "replace",
    );
  }

  const crumbs = buildBreadcrumbs(directory, t("ui.breadcrumb_archive"));
  const narrowCrumbs = collapseBreadcrumbs(crumbs);
  const data = displayData;
  // Server may return quickly with aggregate_status=loading and empty items
  // while a background rebuild converges — never treat that as authoritative empty.
  const serverProjectionLoading =
    data?.aggregate_status === "loading" && (data.items?.length ?? 0) === 0;
  const isInitialLoading =
    (!displayData && (filesQuery.isLoading || filesQuery.isPending)) ||
    serverProjectionLoading;
  const isListingError =
    !displayData && filesQuery.isError && !isBrowserOffline();
  const total = isInitialLoading ? undefined : data?.total;
  const pages =
    total === undefined
      ? null
      : Math.max(1, Math.ceil(total / DEFAULT_PAGE_SIZE));
  const unit =
    data?.mode === "search" ? t("ui.files_found_unit") : t("ui.items_unit");
  const listingFacets = displayData as
    | (FilesResponse & {
        state_counts?: Record<string, number>;
        storage_counts?: Record<string, number>;
      })
    | undefined;
  const stateCounts = listingFacets?.state_counts;
  const storageCounts = listingFacets?.storage_counts;
  const hasFilter = Boolean(q || state || storage);
  const stateFilterOptions = [
    { value: "", label: t("ui.all_items") },
    { value: "local_only", label: t("state.filter.local_only") },
    { value: "both", label: t("state.filter.both") },
    { value: "cloud_only", label: t("state.filter.cloud_only") },
    { value: "restoring", label: t("state.filter.restoring") },
  ].filter(
    (option) =>
      !option.value ||
      option.value === state ||
      !stateCounts ||
      (stateCounts[option.value] ?? 0) > 0,
  );
  const storageFilterOptions = [
    { value: "", label: t("ui.all_storage_classes") },
    { value: "STANDARD", label: t("storage.STANDARD") },
    { value: "STANDARD_IA", label: t("storage.STANDARD_IA") },
    { value: "GLACIER_IR", label: t("storage.GLACIER_IR") },
    { value: "GLACIER", label: t("storage.GLACIER") },
    { value: "DEEP_ARCHIVE", label: t("storage.DEEP_ARCHIVE") },
    { value: "none", label: t("ui.storage_none") },
  ].filter(
    (option) =>
      !option.value ||
      option.value === storage ||
      !storageCounts ||
      (storageCounts[option.value] ?? 0) > 0,
  );
  const emptyMessage = hasFilter
    ? t("ui.empty_no_matches")
    : t("ui.empty_no_files");

  return (
    <div className="min-w-0" data-testid="file-browser">
      <RenameCandidatesPanel
        vaultId={vaultId}
        canOperate={capabilities.can_operate}
        t={t}
      />
      {showingStale ? (
        <div
          role="status"
          data-testid="offline-stale-banner"
          className="mb-3 rounded-lg border border-amber-soft bg-amber-soft px-3 py-2 text-sm text-ink"
        >
          {t("ui.offline_stale_listing")}
        </div>
      ) : null}
      {!displayData && (filesQuery.isError || isBrowserOffline()) ? (
        <div
          role="status"
          data-testid="offline-shell"
          className="mb-3 rounded-lg border border-line bg-canvas px-3 py-6 text-center text-sm text-muted"
        >
          {t("ui.offline_shell")}
        </div>
      ) : null}
      <div
        data-testid="file-browser-sticky"
        className="sticky top-0 z-10 border-b border-line bg-surface py-3"
      >
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <label className="min-w-0 flex-1">
            <span className="sr-only">{t("ui.search_placeholder")}</span>
            <input
              type="search"
              value={qInput}
              onChange={(event) => setQInput(event.target.value)}
              placeholder={t("ui.search_placeholder")}
              className="min-h-11 w-full min-w-0 rounded-lg border border-input bg-surface px-3 text-sm text-ink outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              data-testid="file-search"
            />
          </label>
          <label className="shrink-0">
            <span className="sr-only">{t("ui.filter_by_state")}</span>
            <MenuSelect
              label={t("ui.filter_by_state")}
              value={state}
              onValueChange={changeState}
              data-testid="state-filter"
              className="w-full sm:w-[16rem]"
              options={stateFilterOptions}
            />
          </label>
          <label className="shrink-0">
            <span className="sr-only">{t("ui.filter_by_storage")}</span>
            <MenuSelect
              label={t("ui.filter_by_storage")}
              value={storage}
              onValueChange={changeStorage}
              data-testid="storage-filter"
              className="w-full sm:w-[16rem]"
              options={storageFilterOptions}
            />
          </label>
        </div>

        <div className="mt-2 flex min-w-0 items-center gap-2">
          <Button
            type="button"
            variant="secondary"
            className="shrink-0"
            disabled={!directory}
            aria-label={t("ui.go_up")}
            data-testid="up-directory"
            onClick={() => navigateDirectory(parentDirectory(directory))}
          >
            {t("ui.up")}
          </Button>
          <nav
            aria-label={t("ui.breadcrumb_archive")}
            data-testid="breadcrumbs"
            className="min-w-0 flex-1 overflow-hidden"
          >
            {/* Narrow: collapsed trail — no horizontal scroll */}
            <ol
              className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-1 text-sm md:hidden"
              data-testid="breadcrumbs-narrow"
            >
              {narrowCrumbs.map((crumb, index) => {
                if (isBreadcrumbEllipsis(crumb)) {
                  return (
                    <li key={`ellipsis-${index}`} className="px-1 text-muted" aria-hidden="true">
                      …
                    </li>
                  );
                }
                const current = index === narrowCrumbs.length - 1;
                return (
                  <li key={`n-${crumb.path}`} className="flex min-w-0 items-center gap-1">
                    {index > 0 ? (
                      <span className="shrink-0 text-muted" aria-hidden="true">
                        /
                      </span>
                    ) : null}
                    <button
                      type="button"
                      data-directory={crumb.path}
                      disabled={current}
                      aria-current={current ? "page" : undefined}
                      className="max-w-[7rem] truncate font-bold text-ink disabled:cursor-default disabled:text-muted"
                      onClick={() => navigateDirectory(crumb.path)}
                    >
                      {crumb.name}
                    </button>
                  </li>
                );
              })}
            </ol>
            {/* md+: full trail */}
            <ol
              className="hidden min-w-0 flex-wrap items-center gap-x-1 gap-y-1 text-sm md:flex"
              data-testid="breadcrumbs-wide"
            >
              {crumbs.map((crumb, index) => {
                const current = index === crumbs.length - 1;
                return (
                  <li key={`w-${crumb.path || "root"}`} className="flex min-w-0 items-center gap-1">
                    {index > 0 ? (
                      <span className="shrink-0 text-muted" aria-hidden="true">
                        /
                      </span>
                    ) : null}
                    <button
                      type="button"
                      data-directory={crumb.path}
                      disabled={current}
                      aria-current={current ? "page" : undefined}
                      className="max-w-[12rem] truncate font-bold text-ink disabled:cursor-default disabled:text-muted"
                      onClick={() => navigateDirectory(crumb.path)}
                    >
                      {crumb.name}
                    </button>
                  </li>
                );
              })}
            </ol>
          </nav>
        </div>
      </div>

      <div className="min-w-0 overflow-x-hidden pt-4">
        {isInitialLoading ? (
          <div
            className="space-y-2"
            data-testid="file-list-loading"
            role="status"
            aria-live="polite"
            aria-busy="true"
          >
            <p className="text-sm text-muted">{t("ui.file_list_loading")}</p>
            <div className="h-12 animate-pulse rounded-lg bg-canvas" />
            <div className="h-12 animate-pulse rounded-lg bg-canvas" />
            <div className="h-12 animate-pulse rounded-lg bg-canvas" />
          </div>
        ) : null}

        {isListingError ? (
          <div
            role="alert"
            data-testid="file-list-error"
            className="rounded-lg border border-line bg-canvas px-3 py-6 text-center"
          >
            <p className="text-sm text-muted">{t("ui.file_list_error")}</p>
            <Button
              type="button"
              variant="secondary"
              className="mt-3"
              data-testid="file-list-retry"
              onClick={() => {
                void filesQuery.refetch();
              }}
            >
              {t("ui.file_list_retry")}
            </Button>
          </div>
        ) : null}

        {data &&
        data.items.length === 0 &&
        !isInitialLoading &&
        data.aggregate_status !== "loading" ? (
          <p
            className="py-8 text-center text-sm text-muted"
            data-testid="file-list-empty"
            data-empty={hasFilter ? "no-matches" : "no-files"}
          >
            {emptyMessage}
          </p>
        ) : null}

        {data && data.items.length > 0 && !serverProjectionLoading ? (
          <FileOperationsHost
            items={data.items}
            capabilities={capabilities}
            vaultName={vaultName}
            t={t}
            sheetPath={sheetPath}
            onSheetPathChange={setSheetPath}
            demoConfirm={
              demoConfirm
                ? { action: demoConfirm, path: demoConfirmTarget }
                : null
            }
            demoVersionsPath={demoVersions}
          >
            {({
              jobsByPath,
              onOpenActions,
              onDesktopAction,
              onCancelJob,
              onApproveJob,
              onAcceleratePurge,
              cancelBusyId,
              approveBusyId,
              accelerateBusyId,
            }) => (
              <FileList
                items={data.items}
                t={t}
                capabilities={capabilities}
                sortKey={sortKey}
                sortOrder={sortOrder}
                onSort={changeSort}
                onOpenDirectory={(path) => navigateDirectory(path)}
                onOpenFile={(path) => setHistoryPath(path)}
                onOpenActions={onOpenActions}
                onDesktopAction={onDesktopAction}
                jobsByPath={jobsByPath}
                onCancelJob={onCancelJob}
                onApproveJob={onApproveJob}
                onAcceleratePurge={onAcceleratePurge}
                cancelBusyId={cancelBusyId}
                approveBusyId={approveBusyId}
                accelerateBusyId={accelerateBusyId}
              />
            )}
          </FileOperationsHost>
        ) : null}

        {historyPath ? (
          <PathHistoryPanel
            path={historyPath}
            t={t}
            onClose={() => setHistoryPath(null)}
          />
        ) : null}

        <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm text-muted" data-testid="page-label">
            {total === undefined || pages === null
              ? t("ui.file_list_loading")
              : t("ui.page_label", {
                  page,
                  pages,
                  total: total.toLocaleString("en-US"),
                  unit,
                })}
          </p>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="secondary"
              disabled={page <= 1 || total === undefined}
              data-testid="page-previous"
              onClick={() => changePage(page - 1)}
            >
              {t("ui.previous")}
            </Button>
            <Button
              type="button"
              variant="secondary"
              disabled={
                total === undefined || pages === null || page >= pages
              }
              data-testid="page-next"
              onClick={() => changePage(page + 1)}
            >
              {t("ui.next")}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
