import { useCallback, useEffect, useState } from "react";

import { StopProtectionDialog } from "@/components/StopProtectionDialog";
import { Panel } from "@/components/Panel";
import { Toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

import { LifecyclePanel } from "./LifecyclePanel";
import { OperationPolicyPanel } from "./OperationPolicyPanel";
import { RetentionPanel } from "./RetentionPanel";

export type VaultAccessPageProps = {
  vaultId: number;
  vaultName: string;
  protectionState?: "active" | "decommissioning" | "decommissioned";
  onBack?: () => void;
  onStopped?: () => void;
};

export function VaultAccessPage({
  vaultId,
  vaultName,
  protectionState = "active",
  onBack,
  onStopped,
}: VaultAccessPageProps) {
  const { t, ready } = useI18n();
  const [notice, setNotice] = useState<{
    message: string;
    error: boolean;
  } | null>(null);
  const [stopOpen, setStopOpen] = useState(protectionState !== "active");
  const [stoppedHere, setStoppedHere] = useState(false);

  const show = useCallback((message: string, error = false) => {
    setNotice({ message, error });
  }, []);

  useEffect(() => {
    if (protectionState !== "active") setStopOpen(true);
  }, [protectionState]);

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

      {protectionState !== "active" ? (
        <section data-panel="stop-protection-status">
          <Panel className="border-red-300 p-4 sm:p-5">
            <h2 className="text-lg font-bold text-ink">
              {t("stop_protection.status")}
            </h2>
            <p className="mt-1 text-sm text-muted">
              {t(
                protectionState === "decommissioned"
                  ? "stop_protection.stopped"
                  : "stop_protection.pending",
              )}
            </p>
            <Button
              type="button"
              variant="primary"
              className="mt-4 min-h-11 w-full sm:w-auto"
              onClick={() => setStopOpen(true)}
            >
              {t("stop_protection.status")}
            </Button>
          </Panel>
        </section>
      ) : (
        <>
      <RetentionPanel onNotice={show} />
      <OperationPolicyPanel onNotice={show} />
      <details className="min-w-0">
        <summary className="cursor-pointer py-3 font-semibold">{t("access.advanced")}</summary>
        <LifecyclePanel key={vaultId} vaultId={vaultId} onNotice={show} />
      </details>

      <section data-panel="stop-protection">
        <Panel className="border-red-300 p-4 sm:p-5">
          <h2 className="text-lg font-bold text-ink">{t("stop_protection.title")}</h2>
          <p className="mt-1 text-sm text-muted">{t("stop_protection.local_retained")}</p>
          <Button
            type="button"
            variant="danger"
            className="mt-4 min-h-11 w-full sm:w-auto"
            onClick={() => setStopOpen(true)}
          >
            {t("stop_protection.title")}
          </Button>
        </Panel>
      </section>
        </>
      )}

      <StopProtectionDialog
        open={stopOpen}
        onOpenChange={(open) => {
          setStopOpen(open);
          if (!open && stoppedHere) {
            if (onStopped) onStopped();
            else window.location.href = "/no-vault";
          }
        }}
        vaultName={vaultName}
        vaultId={vaultId}
        onCompleted={() => setStoppedHere(true)}
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
