import { useCallback, useEffect, useState } from "react";

import type { AdminVault } from "@/api";
import {
  cancelOwnVaultDecommissionCloudPurge,
  fetchAdminVaults,
  fetchOwnVaultDecommissionStatus,
  previewOwnVaultDecommission,
  startOwnVaultDecommission,
} from "@/api";
import { Badge } from "@/components/Badge";
import { BottomSheet } from "@/components/BottomSheet";
import { DecommissionVaultDialog } from "@/components/DecommissionVaultDialog";
import { Panel } from "@/components/Panel";
import { Toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

import { AdminAuditEventsSection } from "./AuditEventsSection";
import { NotificationsSection } from "./NotificationsSection";
import { RelocateVaultDialog } from "./RelocateVaultDialog";
import { SettingsSection } from "./SettingsSection";
import { SourceVolumesSection } from "./SourceVolumesSection";
import { MetadataBackupsSection } from "./MetadataBackupsSection";
import { StorageCostEstimatesSection } from "./StorageCostEstimatesSection";
import { WorkerErrorsSection } from "./WorkerErrorsSection";

type NoticeState = {
  open: boolean;
  message: string;
  error: boolean;
};

/** Sections with their own component; every other admin path lists Vaults. */
const DEDICATED_SECTION_PATHS = new Set([
  "/admin/storage-cost-estimates",
  "/admin/audit-events",
  "/admin/worker-errors",
  "/admin/notifications",
  "/admin/metadata-backups",
  "/admin/defaults",
  "/admin/deployment",
  "/admin/sources",
]);

export function AdminPage() {
  const { t, ready } = useI18n();
  const pathname = typeof window === "undefined" ? "/admin" : window.location.pathname;

  const [vaults, setVaults] = useState<AdminVault[]>([]);
  const [notice, setNotice] = useState<NoticeState>({
    open: false,
    message: "",
    error: false,
  });

  const [relocateVault, setRelocateVault] = useState<AdminVault | null>(null);
  const [decommissionVault, setDecommissionVault] = useState<AdminVault | null>(null);
  const [vaultSheetOpen, setVaultSheetOpen] = useState(false);
  const [vaultSheetTarget, setVaultSheetTarget] = useState<AdminVault | null>(
    null,
  );

  const showNotice = useCallback((message: string, error = false) => {
    setNotice({ open: true, message, error });
  }, []);

  const loadVaults = useCallback(async () => {
    const data = await fetchAdminVaults();
    setVaults(data.items);
  }, []);

  useEffect(() => {
    if (DEDICATED_SECTION_PATHS.has(pathname)) return;
    let cancelled = false;
    void loadVaults().catch((error: unknown) => {
      if (!cancelled) {
        showNotice(error instanceof Error ? error.message : String(error), true);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [pathname, loadVaults, showNotice]);

  if (!ready) {
    return (
      <main className="mx-auto w-[min(1180px,calc(100%-2rem))] py-6">
        <p className="text-muted">{t("admin.title")}</p>
      </main>
    );
  }

  const settingsMode = pathname === "/admin/defaults"
    ? "defaults"
    : pathname === "/admin/deployment"
      ? "deployment"
      : null;

  return (
    <div className="min-h-svh bg-canvas text-ink">
      <main
        id="main-content"
        className="mx-auto w-[min(1180px,calc(100%-2rem))] py-6"
      >
        <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="text-xs font-extrabold tracking-[0.16em] text-green uppercase">
              {t("ui.product_name")}
            </p>
            <h1 className="text-2xl font-bold tracking-tight md:text-[27px]">
              {t("admin.heading")}
            </h1>
            <p className="mt-1 text-sm text-muted">{t("admin.subtitle")}</p>
          </div>
          <a
            href="/"
            className="inline-flex min-h-11 items-center rounded-[10px] border border-input bg-surface px-4 font-bold transition-[background-color,border-color,box-shadow] duration-150 hover:border-[var(--interactive-border-hover)] hover:bg-green-soft focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 motion-reduce:transition-none"
          >
            {t("admin.back_to_archive")}
          </a>
        </header>

        <nav
          aria-label={t("admin.sections_label")}
          className="mb-6 overflow-x-auto rounded-panel border border-line bg-surface p-2"
        >
          <ul className="flex min-w-max gap-1" role="list">
            {[
              ["admin.section_overview", "/admin"],
              ["admin.section_vaults", "/admin/vaults"],
              ["admin.section_sources", "/admin/sources"],
              ["admin.section_defaults", "/admin/defaults"],
              ["admin.section_notifications", "/admin/notifications"],
              ["admin.section_deployment", "/admin/deployment"],
              ["admin.section_storage_estimates", "/admin/storage-cost-estimates"],
              ["admin.section_audit_events", "/admin/audit-events"],
              ["admin.section_worker_errors", "/admin/worker-errors"],
              ["admin.section_metadata_backups", "/admin/metadata-backups"],
            ].map(([labelKey, href]) => (
              <li key={href}>
                <a
                  href={href}
                  aria-current={pathname === href ? "page" : undefined}
                  className="inline-flex min-h-11 items-center rounded-[10px] px-3 text-sm font-bold text-muted transition-colors duration-150 hover:bg-green-soft hover:text-ink focus-visible:bg-green-soft focus-visible:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-green aria-[current=page]:bg-green-soft aria-[current=page]:text-ink motion-reduce:transition-none"
                >
                  {t(labelKey)}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        {pathname === "/admin/storage-cost-estimates" ? (
          <StorageCostEstimatesSection />
        ) : pathname === "/admin/audit-events" ? (
          <AdminAuditEventsSection />
        ) : pathname === "/admin/worker-errors" ? (
          <WorkerErrorsSection />
        ) : pathname === "/admin/notifications" ? (
          <NotificationsSection />
        ) : pathname === "/admin/metadata-backups" ? (
          <MetadataBackupsSection />
        ) : settingsMode ? (
          <SettingsSection mode={settingsMode} />
        ) : pathname === "/admin/sources" ? (
          <SourceVolumesSection />
        ) : (
          <section>
          <Panel className="p-5">
            <h2 className="mb-4 text-lg font-bold">{t("admin.vaults")}</h2>
            <ul className="grid gap-2">
              {vaults.map((vault) => (
                <li
                  key={vault.id}
                  className="flex flex-wrap items-center justify-between gap-3 border-b border-line py-3"
                >
                  <div className="min-w-0">
                    <strong className="block truncate">{vault.name}</strong>
                    <small className="text-muted">
                      {vault.slug} · {vault.source_root}
                    </small>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      state={
                        vault.decommission_state === "decommissioning"
                          ? "restoring"
                          : vault.decommission_state === "decommissioned"
                            ? "missing"
                            : vault.enabled
                              ? "both"
                              : "missing"
                      }
                      label={
                        vault.decommission_state === "decommissioning"
                          ? t("decommission.state.decommissioning")
                          : vault.decommission_state === "decommissioned"
                            ? t("decommission.state.decommissioned")
                            : vault.enabled
                              ? t("admin.active")
                              : t("admin.disabled")
                      }
                    />
                    <Button
                      type="button"
                      variant="secondary"
                      className="md:hidden"
                      aria-label={t("admin.row_actions")}
                      onClick={() => {
                        setVaultSheetTarget(vault);
                        setVaultSheetOpen(true);
                      }}
                    >
                      ⋯
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      className="max-md:hidden"
                      disabled={(vault.decommission_state ?? "active") !== "active"}
                      onClick={() => setRelocateVault(vault)}
                    >
                      {t("admin.relocate_vault")}
                    </Button>
                    <Button
                      type="button"
                      variant="danger"
                      className="max-md:hidden"
                      onClick={() => setDecommissionVault(vault)}
                    >
                      {(vault.decommission_state ?? "active") === "active"
                        ? t("decommission.open")
                        : t("decommission.view_progress")}
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          </Panel>
          </section>
        )}
      </main>

      <DecommissionVaultDialog
        open={decommissionVault !== null}
        onOpenChange={(open) => {
          if (!open) setDecommissionVault(null);
        }}
        vaultName={decommissionVault?.name ?? ""}
        existingState={decommissionVault?.decommission_state ?? "active"}
        preview={(selection) =>
          previewOwnVaultDecommission(decommissionVault!.id, selection)
        }
        start={(payload) =>
          startOwnVaultDecommission(decommissionVault!.id, payload)
        }
        status={() =>
          fetchOwnVaultDecommissionStatus(decommissionVault!.id)
        }
        cancelCloudPurge={() =>
          cancelOwnVaultDecommissionCloudPurge(decommissionVault!.id)
        }
        onCompleted={loadVaults}
      />

      <RelocateVaultDialog
        open={relocateVault !== null}
        onOpenChange={(open) => {
          if (!open) setRelocateVault(null);
        }}
        vault={relocateVault}
        onNotice={showNotice}
        onCompleted={loadVaults}
      />

      <BottomSheet
        open={vaultSheetOpen}
        onOpenChange={setVaultSheetOpen}
        title={vaultSheetTarget?.name ?? t("admin.row_actions")}
        actions={[
          ...((vaultSheetTarget?.decommission_state ?? "active") === "active"
            ? [
                { id: "relocate", label: t("admin.relocate_vault") },
              ]
            : []),
          {
            id: "decommission",
            label:
              (vaultSheetTarget?.decommission_state ?? "active") === "active"
                ? t("decommission.open")
                : t("decommission.view_progress"),
          },
        ]}
        onAction={(actionId) => {
          if (actionId === "relocate" && vaultSheetTarget) {
            setRelocateVault(vaultSheetTarget);
          }
          if (actionId === "decommission" && vaultSheetTarget) {
            setDecommissionVault(vaultSheetTarget);
          }
        }}
      />

      <Toast
        open={notice.open}
        message={notice.message}
        variant={notice.error ? "error" : "success"}
        onClose={() => setNotice((n) => ({ ...n, open: false }))}
      />
    </div>
  );
}
