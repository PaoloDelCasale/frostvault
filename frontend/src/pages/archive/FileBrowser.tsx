import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { DEFAULT_PAGE_SIZE, filesQueryOptions } from "@/api";
import { Button } from "@/components/ui/button";

import { FileList } from "./FileList";
import {
  buildBreadcrumbs,
  collapseBreadcrumbs,
  parentDirectory,
} from "./fileLabels";
import { PathHistoryPanel } from "./PathHistoryPanel";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type FileBrowserProps = {
  t: Translate;
};

export const SEARCH_DEBOUNCE_MS = 250;

function readSearchParams(): {
  directory: string;
  q: string;
  state: string;
  page: number;
} {
  const params = new URLSearchParams(window.location.search);
  const pageRaw = Number(params.get("page") || "1");
  return {
    directory: params.get("directory") || "",
    q: params.get("q") || "",
    state: params.get("state") || "",
    page: Number.isFinite(pageRaw) && pageRaw >= 1 ? pageRaw : 1,
  };
}

function writeSearchParams(
  next: {
    directory: string;
    q: string;
    state: string;
    page: number;
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
  if (next.page > 1) url.searchParams.set("page", String(next.page));
  else url.searchParams.delete("page");
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
export function FileBrowser({ t }: FileBrowserProps) {
  const initial = readSearchParams();
  const [directory, setDirectory] = useState(initial.directory);
  const [qInput, setQInput] = useState(initial.q);
  const [q, setQ] = useState(initial.q);
  const [state, setState] = useState(initial.state);
  const [page, setPage] = useState(initial.page);
  const [historyPath, setHistoryPath] = useState<string | null>(() => {
    // Screenshot / deep-link helper: ?history=<path> opens Path History.
    return new URLSearchParams(window.location.search).get("history");
  });

  const query = useMemo(
    () => ({
      q,
      state,
      directory,
      page,
      page_size: DEFAULT_PAGE_SIZE,
    }),
    [q, state, directory, page],
  );

  const filesQuery = useQuery(filesQueryOptions(query));

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (qInput === q) return;
      setQ(qInput);
      setPage(1);
      writeSearchParams({ directory, q: qInput, state, page: 1 }, "replace");
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [qInput, q, directory, state]);

  useEffect(() => {
    const onPopState = () => {
      const next = readSearchParams();
      setDirectory(next.directory);
      setQInput(next.q);
      setQ(next.q);
      setState(next.state);
      setPage(next.page);
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
    setPage(1);
    setHistoryPath(null);
    writeSearchParams({ directory: path, q: "", state, page: 1 }, historyMode);
  }

  function changeState(nextState: string) {
    setState(nextState);
    setPage(1);
    writeSearchParams({ directory, q, state: nextState, page: 1 }, "replace");
  }

  function changePage(nextPage: number) {
    setPage(nextPage);
    writeSearchParams({ directory, q, state, page: nextPage }, "replace");
  }

  const crumbs = buildBreadcrumbs(directory, t("ui.breadcrumb_archive"));
  const narrowCrumbs = collapseBreadcrumbs(crumbs);
  const data = filesQuery.data;
  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / DEFAULT_PAGE_SIZE));
  const unit =
    data?.mode === "search" ? t("ui.files_found_unit") : t("ui.items_unit");
  const hasFilter = Boolean(q || state);
  const emptyMessage = hasFilter
    ? t("ui.empty_no_matches")
    : t("ui.empty_no_files");

  return (
    <div className="min-w-0" data-testid="file-browser">
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
              className="min-h-11 w-full min-w-0 rounded-lg border border-input bg-white px-3 text-sm text-ink outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              data-testid="file-search"
            />
          </label>
          <label className="shrink-0">
            <span className="sr-only">{t("ui.filter_by_state")}</span>
            <select
              value={state}
              onChange={(event) => changeState(event.target.value)}
              className="min-h-11 w-full rounded-lg border border-input bg-white px-3 text-sm font-bold text-ink sm:w-auto"
              data-testid="state-filter"
              aria-label={t("ui.filter_by_state")}
            >
              <option value="">{t("ui.all_items")}</option>
              <option value="local_only">{t("state.filter.local_only")}</option>
              <option value="both">{t("state.filter.both")}</option>
              <option value="cloud_only">{t("state.filter.cloud_only")}</option>
              <option value="restoring">{t("state.filter.restoring")}</option>
            </select>
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
                if ("ellipsis" in crumb && crumb.ellipsis) {
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
        {filesQuery.isLoading ? (
          <p className="text-sm text-muted" data-testid="file-list-loading">
            {t("ui.file_list_placeholder")}
          </p>
        ) : null}

        {filesQuery.isSuccess && data && data.items.length === 0 ? (
          <p
            className="py-8 text-center text-sm text-muted"
            data-testid="file-list-empty"
            data-empty={hasFilter ? "no-matches" : "no-files"}
          >
            {emptyMessage}
          </p>
        ) : null}

        {filesQuery.isSuccess && data && data.items.length > 0 ? (
          <FileList
            items={data.items}
            t={t}
            onOpenDirectory={(path) => navigateDirectory(path)}
            onOpenFile={(path) => setHistoryPath(path)}
          />
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
            {t("ui.page_label", {
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
              disabled={page <= 1}
              data-testid="page-previous"
              onClick={() => changePage(page - 1)}
            >
              {t("ui.previous")}
            </Button>
            <Button
              type="button"
              variant="secondary"
              disabled={page >= pages}
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
