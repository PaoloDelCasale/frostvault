import { useCallback, useEffect, useState } from "react";

import {
  cancelOwnVaultDecommissionCloudPurge,
  fetchOwnVaultDecommissionStatus,
  previewOwnVaultDecommission,
  startOwnVaultDecommission,
} from "@/api";
import { DecommissionVaultDialog } from "@/components/DecommissionVaultDialog";
import { Panel } from "@/components/Panel";
import { Toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

import { CloudDeletionPanel } from "./CloudDeletionPanel";
import { LifecyclePanel } from "./LifecyclePanel";
import { OperationPolicyPanel } from "./OperationPolicyPanel";
import { RetentionPanel } from "./RetentionPanel";

export type VaultAccessPageProps = {
  vaultId: number;
  vaultName: string;
  decommissionState?: "active" | "decommissioning" | "decommissioned";
  onBack?: () => void;
  onDecommissioned?: () => void;
};

export function VaultAccessPage({
  vaultId,
  vaultName,
  decommissionState = "active",
  onBack,
  onDecommissioned,
}: VaultAccessPageProps) {
  const { t, ready } = useI18n();
  const [notice, setNotice] = useState<{
    message: string;
    error: boolean;
  } | null>(null);
  const [decommissionOpen, setDecommissionOpen] = useState(
    decommissionState !== "active",
  );

  const show = useCallback((message: string, error = false) => {
    setNotice({ message, error });
  }, []);

  useEffect(() => {
    if (decommissionState !== "active") setDecommissionOpen(true);
  }, [decommissionState]);

  if (!ready) {
    return (
      <div className="mx-auto grid w-full min-w-0 max-w-[960px] gap-4 px-3 py-4 sm:px-4">
        <p className="text-sm text-muted" role="status">
          {t("access.title")}
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto grid w-full min-w-0 max-w-[960px] gap-4 px-3 py-4 sm:px-4">
      <header className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="text-[11px] font-black tracking-[0.14em] text-muted uppercase">
            {t("ui.private_archive")}
          </p>
          <h1 className="text-[27px] font-bold text-ink">{vaultName}</h1>
          <p className="mt-1 text-sm text-muted">{t("access.subtitle")}</p>
        </div>
        <Button
          type="button"
          variant="secondary"
          className="min-h-11 self-start"
          onClick={() => {
            if (onBack) onBack();
            else window.location.href = "/";
          }}
        >
          {t("access.back_to_archive")}
        </Button>
      </header>

      {decommissionState !== "active" ? (
        <section data-panel="decommission-progress">
          <Panel className="border-red-300 p-4 sm:p-5">
            <h2 className="text-lg font-bold text-ink">
              {t("decommission.view_progress")}
            </h2>
            <p className="mt-1 text-sm text-muted">
              {t(
                decommissionState === "decommissioned"
                  ? "decommission.root_released"
                  : "decommission.root_reserved",
              )}
            </p>
            <Button
              type="button"
              variant="primary"
              className="mt-4 min-h-11 w-full sm:w-auto"
              onClick={() => setDecommissionOpen(true)}
            >
              {t("decommission.view_progress")}
            </Button>
          </Panel>
        </section>
      ) : (
        <>
      <RetentionPanel onNotice={show} />
      <OperationPolicyPanel onNotice={show} />
      <LifecyclePanel onNotice={show} />
      <CloudDeletionPanel onNotice={show} />

      <section data-panel="decommission">
        <Panel className="border-red-300 p-4 sm:p-5">
          <h2 className="text-lg font-bold text-ink">{t("decommission.owner_panel_title")}</h2>
          <p className="mt-1 text-sm text-muted">{t("decommission.owner_panel_help")}</p>
          <Button
            type="button"
            variant="danger"
            className="mt-4 min-h-11 w-full sm:w-auto"
            onClick={() => setDecommissionOpen(true)}
          >
            {t("decommission.open")}
          </Button>
        </Panel>
      </section>
        </>
      )}

      <DecommissionVaultDialog
        open={decommissionOpen}
        onOpenChange={setDecommissionOpen}
        vaultName={vaultName}
        existingState={decommissionState}
        preview={(selection) => previewOwnVaultDecommission(vaultId, selection)}
        start={(payload) => startOwnVaultDecommission(vaultId, payload)}
        status={() => fetchOwnVaultDecommissionStatus(vaultId)}
        cancelCloudPurge={() => cancelOwnVaultDecommissionCloudPurge(vaultId)}
        onCompleted={() => {
          if (onDecommissioned) onDecommissioned();
          else window.location.href = "/no-vault";
        }}
      />

      <Toast
        open={notice !== null}
        message={notice?.message ?? ""}
        variant={notice?.error ? "error" : "success"}
        onClose={() => setNotice(null)}
      />
    </div>
  );
}
