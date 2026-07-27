import { useEffect, useState } from "react";

import {
  createAdminInvite,
  fetchAdminInvites,
  revokeAdminInvite,
  type AdminInvite,
  type AdminUser,
} from "@/api";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Dialog } from "@/components/Dialog";
import { FormField, FormSelect } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

export function InvitesPanel({ users }: { users: AdminUser[] }) {
  const { t } = useI18n();
  const [invites, setInvites] = useState<AdminInvite[]>([]);
  const [targetUserId, setTargetUserId] = useState("");
  const [issuedToken, setIssuedToken] = useState<string | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<AdminInvite | null>(null);
  const [error, setError] = useState("");

  async function loadInvites() {
    const result = await fetchAdminInvites();
    setInvites(result.items);
  }

  useEffect(() => {
    let cancelled = false;
    void fetchAdminInvites()
      .then((result) => {
        if (!cancelled) setInvites(result.items);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function createInvite() {
    if (!targetUserId) return;
    try {
      const result = await createAdminInvite(Number(targetUserId));
      setIssuedToken(result.token);
      setTargetUserId("");
      await loadInvites();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function confirmRevoke() {
    if (!revokeTarget) return;
    try {
      await revokeAdminInvite(revokeTarget.id);
      setRevokeTarget(null);
      await loadInvites();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setRevokeTarget(null);
    }
  }

  return (
    <>
      <Panel className="p-5">
        <h2 className="text-lg font-bold">{t("admin.invites_heading")}</h2>
        <p className="mt-1 text-sm text-muted">{t("admin.invites_help")}</p>
        {error ? <p role="alert" className="mt-3 text-sm text-red-700">{error}</p> : null}
        <div className="mt-4 flex flex-wrap items-end gap-3">
          <FormField label={t("admin.invite_user")} htmlFor="admin-invite-user" className="min-w-56 flex-1">
            <FormSelect id="admin-invite-user" value={targetUserId} onChange={(event) => setTargetUserId(event.target.value)}>
              <option value="">{t("admin.invite_choose_user")}</option>
              {users.filter((user) => user.active).map((user) => (
                <option key={user.id} value={user.id}>{user.display_name} (@{user.username})</option>
              ))}
            </FormSelect>
          </FormField>
          <Button type="button" variant="primary" disabled={!targetUserId} onClick={() => void createInvite()}>
            {t("admin.invite_create")}
          </Button>
        </div>
        {invites.length === 0 ? (
          <p className="mt-4 text-sm text-muted">{t("admin.invites_empty")}</p>
        ) : (
          <ul className="mt-4 grid gap-2">
            {invites.map((invite) => (
              <li key={invite.id} className="flex flex-wrap items-center justify-between gap-3 border-b border-line py-3">
                <div>
                  <strong className="block">@{invite.target_username}</strong>
                  <small className="text-muted">{t("admin.invite_expires", { date: invite.expires_at })}</small>
                </div>
                <Button type="button" variant="danger" onClick={() => setRevokeTarget(invite)}>{t("admin.invite_revoke")}</Button>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Dialog
        open={issuedToken !== null}
        onOpenChange={(open) => {
          if (!open) setIssuedToken(null);
        }}
        title={t("admin.invite_issued_title")}
        description={t("admin.invite_issued_help")}
      >
        <code className="block break-all rounded-[10px] bg-canvas p-3 text-sm">{issuedToken}</code>
        <div className="mt-4 flex flex-wrap justify-end gap-2">
          <Button type="button" variant="secondary" onClick={() => void navigator.clipboard?.writeText(issuedToken ?? "")}>
            {t("admin.invite_copy")}
          </Button>
          <Button type="button" variant="primary" onClick={() => setIssuedToken(null)}>
            {t("admin.invite_saved")}
          </Button>
        </div>
      </Dialog>

      <ConfirmDialog
        open={revokeTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRevokeTarget(null);
        }}
        title={t("admin.invite_revoke_title")}
        description={t("admin.invite_revoke_help")}
        confirmLabel={t("admin.invite_revoke_confirm")}
        cancelLabel={t("admin.cancel")}
        onConfirm={() => void confirmRevoke()}
      />
    </>
  );
}
