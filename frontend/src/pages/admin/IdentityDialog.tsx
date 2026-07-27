import { useEffect, useState } from "react";

import {
  fetchAdminIdentities,
  unlinkAdminIdentity,
  type AdminIdentity,
} from "@/api";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Dialog } from "@/components/Dialog";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

export function IdentityDialog({
  open,
  onOpenChange,
  userId,
  userName,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  userId: number;
  userName: string;
}) {
  const { t } = useI18n();
  const [identities, setIdentities] = useState<AdminIdentity[]>([]);
  const [unlinkTarget, setUnlinkTarget] = useState<AdminIdentity | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setIdentities([]);
    setError("");
    void fetchAdminIdentities(userId)
      .then((result) => {
        if (!cancelled) setIdentities(result.items);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      cancelled = true;
    };
  }, [open, userId]);

  async function confirmUnlink() {
    if (!unlinkTarget) return;
    try {
      const result = await unlinkAdminIdentity(userId, unlinkTarget.id);
      setIdentities(result.items);
      setUnlinkTarget(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setUnlinkTarget(null);
    }
  }

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={onOpenChange}
        title={t("admin.identities_title", { name: userName })}
        description={t("admin.identities_help")}
      >
        {error ? <p role="alert" className="mb-3 text-sm text-red-700">{error}</p> : null}
        {identities.length === 0 ? (
          <p className="text-sm text-muted">{t("admin.identities_empty")}</p>
        ) : (
          <ul className="grid gap-3">
            {identities.map((identity) => (
              <li key={identity.id} className="flex flex-wrap items-center justify-between gap-3 border-b border-line pb-3">
                <div className="min-w-0">
                  <strong className="block break-all">{identity.issuer}</strong>
                  <code className="block break-all text-xs text-muted">{identity.subject}</code>
                </div>
                <Button type="button" variant="danger" onClick={() => setUnlinkTarget(identity)}>
                  {t("admin.identity_unlink")}
                </Button>
              </li>
            ))}
          </ul>
        )}
      </Dialog>
      <ConfirmDialog
        open={unlinkTarget !== null}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) setUnlinkTarget(null);
        }}
        title={t("admin.identity_unlink_title")}
        description={t("admin.identity_unlink_help")}
        confirmLabel={t("admin.identity_unlink_confirm")}
        cancelLabel={t("admin.cancel")}
        onConfirm={() => void confirmUnlink()}
      />
    </>
  );
}
