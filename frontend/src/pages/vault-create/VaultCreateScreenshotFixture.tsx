import { useMemo, useState } from "react";

import { AuthCard } from "@/components/AuthCard";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";

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
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [custodyConfirmed, setCustodyConfirmed] = useState(false);
  const material = useMemo(() => SAMPLE_EXPORT, []);

  return (
    <main className="grid min-h-screen place-items-center bg-canvas px-4 py-[30px]">
      <div className="w-full max-w-[min(440px,100%)]">
        <AuthCard>
          <p className="m-0 mb-1.5 text-[12px] font-extrabold tracking-[0.16em] text-green">
            FROSTVAULT
          </p>
          <h1 className="m-0 text-[clamp(1.75rem,5vw,2.25rem)] font-bold tracking-[-0.04em] text-ink">
            New vault
          </h1>
          <p className="mt-2 text-sm text-muted">
            Create a private archive for Ada Lovelace.
          </p>
          <RecoveryExportPanel
            recoveryExport={material}
            title="Save your recovery export"
            subtitle="Store this Rclone configuration offline. Uploads stay blocked until you confirm custody of the recovery secret."
            exportLabel="Recovery export"
            warning="Uploads stay blocked until you confirm custody of the recovery secret. Save the export before confirming — confirmation is irreversible."
            copyLabel="Copy"
            downloadLabel="Download"
            showWarning={!custodyConfirmed}
          >
            {!custodyConfirmed ? (
              <div className="flex flex-wrap gap-2.5">
                <Button
                  type="button"
                  variant="primary"
                  onClick={() => setConfirmOpen(true)}
                >
                  I saved it — confirm custody
                </Button>
              </div>
            ) : null}
          </RecoveryExportPanel>
        </AuthCard>
      </div>
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Confirm recovery custody?"
        description="This cannot be undone. If you have not saved the recovery export offline, the vault material is unrecoverable."
        confirmLabel="Confirm custody"
        cancelLabel="Cancel"
        tone="danger"
        onConfirm={() => {
          setCustodyConfirmed(true);
          setConfirmOpen(false);
        }}
      />
    </main>
  );
}
