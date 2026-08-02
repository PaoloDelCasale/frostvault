import { useEffect, useState, type FormEvent } from "react";

import type { AdminVault } from "@/api";
import { browseAdminSourceVolume, relocateAdminVault } from "@/api";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { SourceDirectoryBrowser } from "@/components/SourceDirectoryBrowser";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

type Props = {
  vault: AdminVault | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onNotice: (message: string, error?: boolean) => void;
  onCompleted: () => Promise<void>;
};

function customVolumeAlias(sourceRoot: string): string | null {
  const parts = sourceRoot.split("/").filter(Boolean);
  return parts[0] === "sources" && parts[1] && parts[1] !== "managed"
    ? parts[1]
    : null;
}

export function RelocateVaultDialog({
  vault,
  open,
  onOpenChange,
  onNotice,
  onCompleted,
}: Props) {
  const { t } = useI18n();
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const alias = vault ? customVolumeAlias(vault.source_root) : null;

  useEffect(() => {
    if (open) {
      setSelectedPath(null);
      setReason("");
    }
  }, [open, vault?.id]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!vault || !alias || selectedPath === null || reason.trim().length < 3) return;
    setSubmitting(true);
    try {
      await relocateAdminVault(vault.id, {
        volume_alias: alias,
        relative_path: selectedPath,
        reason: reason.trim(),
      });
      onOpenChange(false);
      onNotice(t("admin.relocation_started"));
      await onCompleted();
    } catch (error) {
      onNotice(error instanceof Error ? error.message : String(error), true);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("admin.relocate_vault")}
      description={t("admin.relocation_help")}
    >
      {!alias ? (
        <p role="alert" className="text-sm text-red-700">
          {t("admin.relocation_custom_only")}
        </p>
      ) : (
        <form className="grid gap-4" onSubmit={(event) => void submit(event)}>
          <p className="break-all text-sm text-muted">
            {t("admin.relocation_old_root")}: {vault?.source_root}
          </p>
          <SourceDirectoryBrowser
            volumeAlias={alias}
            browse={(volumeAlias, path) =>
              browseAdminSourceVolume(volumeAlias, path, "adopt")
            }
            selectedPath={selectedPath}
            onSelect={setSelectedPath}
            viewerIsAdmin
          />
          <FormField label={t("admin.relocation_reason")} htmlFor="relocation-reason">
            <FormInput
              id="relocation-reason"
              required
              minLength={3}
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </FormField>
          <p className="text-sm text-muted">{t("admin.relocation_scan_warning")}</p>
          <Button
            type="submit"
            variant="primary"
            disabled={submitting || selectedPath === null || reason.trim().length < 3}
          >
            {submitting ? t("admin.relocation_submitting") : t("admin.relocation_confirm")}
          </Button>
        </form>
      )}
    </Dialog>
  );
}
