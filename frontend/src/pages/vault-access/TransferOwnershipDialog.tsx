import { useState } from "react";

import {
  transferVaultOwner,
  type LatestRequestScope,
  type VaultMember,
} from "@/api";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

type TransferOwnershipDialogProps = {
  open: boolean;
  member: VaultMember | null;
  membersSettled: boolean;
  transferScope: LatestRequestScope;
  onOpenChange: (open: boolean) => void;
  onNotice: (message: string, error?: boolean) => void;
  onTransferred: () => void;
};

export function TransferOwnershipDialog({
  open,
  member,
  membersSettled,
  transferScope,
  onOpenChange,
  onNotice,
  onTransferred,
}: TransferOwnershipDialogProps) {
  const { t } = useI18n();
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!member) return;
    if (!membersSettled) {
      onNotice(t("access.transfer_wait_members"), true);
      return;
    }
    if (confirmation.trim() !== member.username) {
      onNotice(t("access.transfer_mismatch"), true);
      return;
    }
    setBusy(true);
    const handle = transferScope.begin();
    try {
      const result = await handle.settle(transferVaultOwner(member.id));
      if (result === undefined) return;
      onNotice(t("access.transfer_success"));
      setConfirmation("");
      onOpenChange(false);
      onTransferred();
    } catch (error) {
      if (handle.isCurrent()) {
        onNotice(error instanceof Error ? error.message : String(error), true);
      }
    } finally {
      if (handle.isCurrent()) setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setConfirmation("");
        onOpenChange(next);
      }}
      title={t("access.transfer_title")}
      description={
        member
          ? t("access.transfer_message", {
              name: member.display_name,
              username: member.username,
            })
          : undefined
      }
      className="max-w-lg"
    >
      <form className="grid gap-3" onSubmit={(event) => void onSubmit(event)}>
        <FormField
          label={t("access.transfer_type_prompt", {
            username: member?.username ?? "",
          })}
          htmlFor="transfer-confirm-input"
        >
          <FormInput
            id="transfer-confirm-input"
            autoComplete="off"
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
            aria-label={t("access.transfer_confirm_label")}
          />
        </FormField>
        <div className="flex flex-col gap-2 sm:flex-row sm:justify-end">
          <Button
            type="button"
            variant="secondary"
            className="min-h-11 w-full sm:w-auto"
            onClick={() => onOpenChange(false)}
          >
            {t("access.transfer_cancel")}
          </Button>
          <Button
            type="submit"
            variant="danger"
            className="min-h-11 w-full sm:w-auto"
            disabled={busy || !member}
          >
            {t("access.transfer_submit")}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
