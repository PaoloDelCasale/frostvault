import { useCallback, useEffect, useState } from "react";

import {
  assignAdminSourceArea,
  browseAdminSourceVolume,
  fetchAdminSourceAreas,
  fetchAdminUsers,
  revokeAdminSourceArea,
  fetchAdminSourceVolumes,
  type AdminUser,
  type SourceAreaGrant,
  type SourceVolumeInventoryItem,
} from "@/api";
import { SourceDirectoryBrowser } from "@/components/SourceDirectoryBrowser";
import { Panel } from "@/components/Panel";
import { useI18n } from "@/i18n/useI18n";

function displayPath(volumeAlias: string, relativePath: string): string {
  return relativePath
    ? `/sources/${volumeAlias}/${relativePath}`
    : `/sources/${volumeAlias}`;
}

export function SourceVolumesSection() {
  const { t } = useI18n();
  const [items, setItems] = useState<SourceVolumeInventoryItem[] | null>(null);
  const [areas, setAreas] = useState<SourceAreaGrant[]>([]);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [error, setError] = useState("");
  const [assignVolume, setAssignVolume] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [userId, setUserId] = useState("");
  const [reason, setReason] = useState("");
  const [revokeReason, setRevokeReason] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState(false);

  const reload = useCallback(() => {
    return Promise.all([
      fetchAdminSourceVolumes(),
      fetchAdminSourceAreas(),
      fetchAdminUsers(),
    ]).then(([volumes, grants, userList]) => {
      setItems(volumes.items);
      setAreas(grants.items);
      setUsers(userList.items);
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    void reload().catch((reason: unknown) => {
      if (!cancelled) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    });
    return () => {
      cancelled = true;
    };
  }, [reload]);

  const browse = useCallback(
    (volumeAlias: string, path = "") => browseAdminSourceVolume(volumeAlias, path),
    [],
  );

  async function onAssign(volumeAlias: string) {
    if (selectedPath === null || !userId || reason.trim().length < 3) {
      setError(t("admin.source_areas_assign_incomplete"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      await assignAdminSourceArea({
        user_id: Number(userId),
        volume_alias: volumeAlias,
        relative_path: selectedPath,
        reason: reason.trim(),
      });
      setAssignVolume(null);
      setSelectedPath(null);
      setReason("");
      await reload();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function onRevoke(area: SourceAreaGrant) {
    const value = (revokeReason[area.id] || "").trim();
    if (value.length < 3) {
      setError(t("admin.source_areas_reason_required"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      await revokeAdminSourceArea(area.id, value);
      await reload();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  if (!items) {
    return error ? (
      <p role="alert">{error}</p>
    ) : (
      <p className="text-sm text-muted">{t("admin.sources_loading")}</p>
    );
  }

  const userLabel = (id: number) => {
    const match = users.find((user) => user.id === id);
    return match ? `${match.display_name} (${match.username})` : `#${id}`;
  };

  return (
    <section aria-labelledby="admin-sources-heading" className="grid gap-4">
      <div>
        <h2 id="admin-sources-heading" className="text-xl font-bold">
          {t("admin.sources_heading")}
        </h2>
        <p className="mt-1 text-sm text-muted">{t("admin.sources_subtitle")}</p>
      </div>
      {error ? (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      ) : null}
      {items.length === 0 ? (
        <Panel className="p-5">
          <p className="text-sm text-muted">{t("admin.sources_empty")}</p>
          <p className="mt-2 text-sm">{t("admin.sources_nested_mount_help")}</p>
        </Panel>
      ) : (
        <ul className="grid gap-3" role="list">
          {items.map((item) => {
            const volumeAreas = areas.filter(
              (area) => area.volume_alias === item.alias,
            );
            return (
              <li key={item.alias}>
                <Panel className="p-5">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <h3 className="text-lg font-bold">/sources/{item.alias}</h3>
                      <p className="mt-1 break-all text-sm text-muted">{item.path}</p>
                    </div>
                    <span className="rounded-[10px] bg-canvas px-3 py-1 text-xs font-bold uppercase">
                      {t(`admin.sources_health_${item.health}`)}
                    </span>
                  </div>
                  <dl className="mt-4 grid gap-2 text-sm sm:grid-cols-3">
                    <div>
                      <dt className="font-bold text-muted">
                        {t("admin.sources_access")}
                      </dt>
                      <dd>{item.access}</dd>
                    </div>
                    <div>
                      <dt className="font-bold text-muted">
                        {t("admin.sources_vault_count")}
                      </dt>
                      <dd>{item.vault_count}</dd>
                    </div>
                    <div>
                      <dt className="font-bold text-muted">
                        {t("admin.sources_source_area_count")}
                      </dt>
                      <dd>{item.source_area_count}</dd>
                    </div>
                  </dl>
                  {item.diagnostic || item.health !== "ok" ? (
                    <p className="mt-3 text-sm" role="status">
                      {item.diagnostic || t("admin.sources_nested_mount_help")}
                    </p>
                  ) : null}

                  <div className="mt-5 grid gap-3">
                    <h4 className="text-sm font-bold">
                      {t("admin.source_areas_heading")}
                    </h4>
                    {volumeAreas.length === 0 ? (
                      <p className="text-sm text-muted">
                        {t("admin.source_areas_empty")}
                      </p>
                    ) : (
                      <ul className="grid gap-2" role="list">
                        {volumeAreas.map((area) => (
                          <li
                            key={area.id}
                            className="grid gap-2 rounded-[10px] bg-canvas p-3 text-sm"
                          >
                            <div className="flex flex-wrap items-start justify-between gap-2">
                              <div>
                                <p className="font-bold break-all">
                                  {displayPath(area.volume_alias, area.relative_path)}
                                </p>
                                <p className="text-muted">
                                  {userLabel(area.user_id)}
                                  {" · "}
                                  {area.availability === "unavailable"
                                    ? t("admin.source_areas_unavailable")
                                    : t("admin.source_areas_available")}
                                  {!area.usable
                                    ? ` · ${t("admin.source_areas_reserved")}`
                                    : ""}
                                </p>
                              </div>
                            </div>
                            <div className="flex flex-wrap items-end gap-2">
                              <label className="grid min-w-[12rem] flex-1 gap-1">
                                <span className="text-xs font-bold text-muted">
                                  {t("admin.source_areas_reason")}
                                </span>
                                <input
                                  className="rounded-[10px] border border-edge bg-panel px-3 py-2"
                                  value={revokeReason[area.id] || ""}
                                  onChange={(event) =>
                                    setRevokeReason((current) => ({
                                      ...current,
                                      [area.id]: event.target.value,
                                    }))
                                  }
                                />
                              </label>
                              <button
                                type="button"
                                className="rounded-[10px] border border-edge px-3 py-2 text-xs font-bold"
                                disabled={busy}
                                onClick={() => void onRevoke(area)}
                              >
                                {t("admin.source_areas_revoke")}
                              </button>
                            </div>
                          </li>
                        ))}
                      </ul>
                    )}

                    {assignVolume === item.alias ? (
                      <div className="grid gap-3 rounded-[10px] border border-edge p-3">
                        <SourceDirectoryBrowser
                          volumeAlias={item.alias}
                          browse={browse}
                          selectedPath={selectedPath}
                          onSelect={setSelectedPath}
                          viewerIsAdmin
                        />
                        <label className="grid gap-1 text-sm">
                          <span className="font-bold">
                            {t("admin.source_areas_user")}
                          </span>
                          <select
                            className="rounded-[10px] border border-edge bg-panel px-3 py-2"
                            value={userId}
                            onChange={(event) => setUserId(event.target.value)}
                          >
                            <option value="">
                              {t("admin.source_areas_user_placeholder")}
                            </option>
                            {users.map((user) => (
                              <option key={user.id} value={user.id}>
                                {user.display_name} ({user.username})
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="grid gap-1 text-sm">
                          <span className="font-bold">
                            {t("admin.source_areas_reason")}
                          </span>
                          <input
                            className="rounded-[10px] border border-edge bg-panel px-3 py-2"
                            value={reason}
                            onChange={(event) => setReason(event.target.value)}
                          />
                        </label>
                        <div className="flex flex-wrap gap-2">
                          <button
                            type="button"
                            className="rounded-[10px] bg-ink px-3 py-2 text-xs font-bold text-panel"
                            disabled={busy}
                            onClick={() => void onAssign(item.alias)}
                          >
                            {t("admin.source_areas_assign")}
                          </button>
                          <button
                            type="button"
                            className="rounded-[10px] border border-edge px-3 py-2 text-xs font-bold"
                            disabled={busy}
                            onClick={() => {
                              setAssignVolume(null);
                              setSelectedPath(null);
                            }}
                          >
                            {t("admin.source_areas_cancel")}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <button
                        type="button"
                        className="justify-self-start rounded-[10px] border border-edge px-3 py-2 text-xs font-bold"
                        disabled={item.health !== "ok" || busy}
                        onClick={() => {
                          setAssignVolume(item.alias);
                          setSelectedPath(null);
                          setError("");
                        }}
                      >
                        {t("admin.source_areas_assign_open")}
                      </button>
                    )}
                  </div>
                </Panel>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
