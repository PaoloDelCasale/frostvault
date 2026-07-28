import { useEffect, useState } from "react";

import type { SourceDirectoryBrowseResponse, SourceDirectoryEntry } from "@/api";
import { useI18n } from "@/i18n/useI18n";

type BrowseFn = (
  volumeAlias: string,
  path?: string,
) => Promise<SourceDirectoryBrowseResponse>;

type SourceDirectoryBrowserProps = {
  volumeAlias: string;
  browse: BrowseFn;
  selectedPath: string | null;
  onSelect: (relativePath: string) => void;
  viewerIsAdmin: boolean;
};

function occupationLabel(
  entry: SourceDirectoryEntry,
  t: (key: string) => string,
  viewerIsAdmin: boolean,
): string {
  if (!entry.occupation) return "";
  if (!viewerIsAdmin) {
    return entry.occupation.label || t("ui.source_area_occupied_generic");
  }
  const vault = entry.occupation.vault_name || "";
  const owner = entry.occupation.owner_display_name || "";
  return t("ui.source_area_occupied_admin")
    .replace("{vault}", vault)
    .replace("{owner}", owner);
}

export function SourceDirectoryBrowser({
  volumeAlias,
  browse,
  selectedPath,
  onSelect,
  viewerIsAdmin,
}: SourceDirectoryBrowserProps) {
  const { t } = useI18n();
  const [path, setPath] = useState("");
  const [listing, setListing] = useState<SourceDirectoryBrowseResponse | null>(
    null,
  );
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    void browse(volumeAlias, path)
      .then((response) => {
        if (!cancelled) setListing(response);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : String(reason));
          setListing(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [browse, path, volumeAlias]);

  const crumbs = path ? path.split("/") : [];

  return (
    <div
      className="grid gap-3"
      data-testid="source-directory-browser"
      style={{ maxWidth: "100%" }}
    >
      <nav aria-label={t("ui.source_area_browser_breadcrumb")} className="text-sm">
        <ol className="flex flex-wrap items-center gap-1">
          <li>
            <button
              type="button"
              className="font-bold underline-offset-2 hover:underline"
              onClick={() => setPath("")}
            >
              /sources/{volumeAlias}
            </button>
          </li>
          {crumbs.map((crumb, index) => {
            const target = crumbs.slice(0, index + 1).join("/");
            return (
              <li key={target} className="flex items-center gap-1">
                <span aria-hidden="true">/</span>
                <button
                  type="button"
                  className="underline-offset-2 hover:underline"
                  onClick={() => setPath(target)}
                >
                  {crumb}
                </button>
              </li>
            );
          })}
        </ol>
      </nav>

      {error ? (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      ) : null}
      {loading ? (
        <p className="text-sm text-muted">{t("ui.source_area_browser_loading")}</p>
      ) : null}

      {!loading && listing ? (
        listing.items.length === 0 ? (
          <p className="text-sm text-muted">{t("ui.source_area_browser_empty")}</p>
        ) : (
          <ul className="grid gap-1" role="list">
            {listing.items.map((entry) => {
              const selected = selectedPath === entry.relative_path;
              const occupiedText = occupationLabel(entry, t, viewerIsAdmin);
              const disabledSelect = !entry.selectable;
              return (
                <li key={entry.relative_path}>
                  <div
                    className={
                      entry.occupation
                        ? "flex flex-wrap items-center justify-between gap-2 rounded-[10px] bg-canvas px-3 py-2 text-sm text-muted"
                        : "flex flex-wrap items-center justify-between gap-2 rounded-[10px] px-3 py-2 text-sm"
                    }
                  >
                    <div className="min-w-0">
                      {entry.navigable ? (
                        <button
                          type="button"
                          className="font-bold underline-offset-2 hover:underline"
                          onClick={() => setPath(entry.relative_path)}
                        >
                          {entry.name}
                        </button>
                      ) : (
                        <span className="font-bold">{entry.name}</span>
                      )}
                      {occupiedText ? (
                        <p className="mt-0.5 text-xs">{occupiedText}</p>
                      ) : null}
                    </div>
                    <button
                      type="button"
                      disabled={disabledSelect}
                      aria-pressed={selected}
                      className="shrink-0 rounded-[10px] border border-edge px-3 py-1 text-xs font-bold disabled:cursor-not-allowed disabled:opacity-50"
                      onClick={() => onSelect(entry.relative_path)}
                    >
                      {selected
                        ? t("ui.source_area_browser_selected")
                        : t("ui.source_area_browser_select")}
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )
      ) : null}

      {path === "" && !loading ? (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="rounded-[10px] border border-edge px-3 py-1 text-xs font-bold"
            onClick={() => onSelect("")}
            aria-pressed={selectedPath === ""}
          >
            {selectedPath === ""
              ? t("ui.source_area_browser_volume_root_selected")
              : t("ui.source_area_browser_select_volume_root")}
          </button>
        </div>
      ) : null}
    </div>
  );
}
