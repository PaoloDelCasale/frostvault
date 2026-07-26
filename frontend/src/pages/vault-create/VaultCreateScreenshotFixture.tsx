import { useMemo, useState } from "react";

import { AuthCard } from "@/components/AuthCard";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

import { RecoveryExportPanel } from "./RecoveryExportPanel";

const SAMPLE_EXPORT = [
  "# FrostVault recovery export — keep offline",
  "[fv-crypt]",
  "type = crypt",
  "remote = frostvault:bucket/prefix",
  "password = very-long-obscured-password-token-aaaaaaaaaaaaaaaa",
  "password2 = very-long-obscured-salt-token-bbbbbbbbbbbbbbbb",
  "filename_encryption = standard",
  "directory_name_encryption = true",
].join("\n");

/**
 * Capture-only view for the 375px recovery panel screenshot.
 * Activated from App via ?demo=vault-create-recovery.
 */
export function VaultCreateScreenshotFixture() {
  const { t } = useI18n();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [custodyConfirmed, setCustodyConfirmed] = useState(false);
  const material = useMemo(() => SAMPLE_EXPORT, []);

  return (
    <main className="grid min-h-screen place-items-center bg-canvas px-4 py-[30px]">
      <div className="w-full max-w-[min(440px,100%)]">
        <AuthCard>
          <p className="m-0 mb-1.5 text-[12px] font-extrabold tracking-[0.16em] text-green">
            {t("ui.private_archive")}
          </p>
          <h1 className="m-0 text-[clamp(1.75rem,5vw,2.25rem)] font-bold tracking-[-0.04em] text-ink">
            {t("ui.vault_create.title")}
          </h1>
          <p className="mt-2 text-sm text-muted">
            {t("ui.vault_create.subtitle", { name: "Ada Lovelace" })}
          </p>
          <RecoveryExportPanel
            recoveryExport={material}
            title={t("ui.recovery.title")}
            subtitle={t("ui.recovery.subtitle")}
            exportLabel={t("ui.recovery.export_label")}
            warning={t("ui.recovery.warning")}
            copyLabel={t("ui.recovery.copy")}
            downloadLabel={t("ui.recovery.download")}
            showWarning={!custodyConfirmed}
          >
            {!custodyConfirmed ? (
              <div className="flex flex-wrap gap-2.5">
                <Button
                  type="button"
                  variant="primary"
                  onClick={() => setConfirmOpen(true)}
                >
                  {t("ui.recovery.confirm")}
                </Button>
              </div>
            ) : null}
          </RecoveryExportPanel>
        </AuthCard>
      </div>
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={t("ui.recovery.confirm_title")}
        description={t("ui.recovery.confirm_description")}
        confirmLabel={t("ui.recovery.confirm_action")}
        cancelLabel={t("ui.vault_create.cancel")}
        tone="danger"
        onConfirm={() => {
          setCustodyConfirmed(true);
          setConfirmOpen(false);
        }}
      />
    </main>
  );
}
