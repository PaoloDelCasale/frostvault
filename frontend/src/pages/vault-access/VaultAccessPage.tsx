import { useCallback, useEffect, useRef, useState } from "react";

import {
  addVaultMember,
  ApiError,
  cancelOwnVaultDecommissionCloudPurge,
  createLatestRequestScope,
  fetchOwnVaultDecommissionStatus,
  fetchVaultMembers,
  lookupVaultUser,
  previewOwnVaultDecommission,
  removeVaultMember,
  startOwnVaultDecommission,
  type UserLookupResult,
  type VaultMember,
} from "@/api";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DecommissionVaultDialog } from "@/components/DecommissionVaultDialog";
import { FormField, FormInput, FormSelect } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

import { CloudDeletionPanel } from "./CloudDeletionPanel";
import { LifecyclePanel } from "./LifecyclePanel";
import { OperationPolicyPanel } from "./OperationPolicyPanel";
import { QuotasPanel } from "./QuotasPanel";
import { RetentionPanel } from "./RetentionPanel";
import { TransferOwnershipDialog } from "./TransferOwnershipDialog";

export type VaultAccessPageProps = {
  vaultId: number;
  vaultName: string;
  decommissionState?: "active" | "decommissioning" | "decommissioned";
  isAdmin?: boolean;
  onBack?: () => void;
  onTransferred?: () => void;
};

function roleKey(role: string | null | undefined): string {
  if (role === "owner") return "access.role_owner";
  if (role === "operator") return "access.role_operator";
  if (role === "viewer") return "access.role_viewer";
  return "access.role_none";
}

export function VaultAccessPage({
  vaultId,
  vaultName,
  decommissionState = "active",
  isAdmin = false,
  onBack,
  onTransferred,
}: VaultAccessPageProps) {
  const { t, ready } = useI18n();
  const [notice, setNotice] = useState<{
    message: string;
    error: boolean;
  } | null>(null);
  const [lookupUsername, setLookupUsername] = useState("");
  const [lookupUser, setLookupUser] = useState<UserLookupResult | null>(null);
  const [lookupRole, setLookupRole] = useState("viewer");
  const [lookupBusy, setLookupBusy] = useState(false);
  const [members, setMembers] = useState<VaultMember[]>([]);
  const [membersBusy, setMembersBusy] = useState(false);
  const [removeTarget, setRemoveTarget] = useState<VaultMember | null>(null);
  const [transferTarget, setTransferTarget] = useState<VaultMember | null>(
    null,
  );
  const [decommissionOpen, setDecommissionOpen] = useState(
    decommissionState !== "active",
  );

  const membersScope = useRef(createLatestRequestScope()).current;
  const transferScope = useRef(createLatestRequestScope()).current;

  const show = useCallback((message: string, error = false) => {
    setNotice({ message, error });
  }, []);

  const loadMembers = useCallback(
    async (opts?: { announce?: boolean }) => {
      setMembersBusy(true);
      transferScope.begin();
      const handle = membersScope.begin();
      try {
        const data = await handle.settle(fetchVaultMembers());
        if (data === undefined) return;
        setMembers(data.items);
        if (opts?.announce) show(t("access.members_refreshed"));
      } catch (error) {
        if (handle.isCurrent()) {
          show(error instanceof Error ? error.message : String(error), true);
        }
      } finally {
        if (handle.isCurrent()) setMembersBusy(false);
      }
    },
    [membersScope, show, t, transferScope],
  );

  useEffect(() => {
    if (!ready || decommissionState !== "active") return;
    void loadMembers();
  }, [decommissionState, loadMembers, ready]);

  useEffect(() => {
    if (decommissionState !== "active") setDecommissionOpen(true);
  }, [decommissionState]);

  if (!ready) {
    return (
      <div className="mx-auto grid w-full max-w-[960px] gap-4 px-3 py-4 sm:px-4">
        <p className="text-sm text-muted" role="status">
          {t("access.title")}
        </p>
      </div>
    );
  }

  async function onLookup(event: React.FormEvent) {
    event.preventDefault();
    setLookupBusy(true);
    try {
      const result = await lookupVaultUser(lookupUsername.trim());
      setLookupUser(result);
      setLookupRole(result.current_vault_role === "operator" ? "operator" : "viewer");
    } catch (error) {
      setLookupUser(null);
      const message =
        error instanceof ApiError && error.status === 404
          ? t("access.lookup_not_found")
          : error instanceof Error
            ? error.message
            : String(error);
      show(message, true);
    } finally {
      setLookupBusy(false);
    }
  }

  async function onGrantAccess(event: React.FormEvent) {
    event.preventDefault();
    if (!lookupUser) return;
    try {
      await addVaultMember(lookupUser.id, lookupRole);
      show(t("access.access_updated"));
      setLookupUser({ ...lookupUser, current_vault_role: lookupRole as "operator" | "viewer" });
      await loadMembers();
    } catch (error) {
      show(error instanceof Error ? error.message : String(error), true);
    }
  }

  async function confirmRemove() {
    if (!removeTarget) return;
    const target = removeTarget;
    setRemoveTarget(null);
    try {
      await removeVaultMember(target.id);
      show(t("access.access_removed"));
      await loadMembers();
    } catch (error) {
      show(error instanceof Error ? error.message : String(error), true);
    }
  }

  return (
    <div className="mx-auto grid w-full max-w-[960px] gap-4 px-3 py-4 sm:px-4">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
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
      <section data-panel="add-member">
      <Panel className="p-4 sm:p-5">
        <h2 className="text-lg font-bold">{t("access.add_member")}</h2>
        <p className="mt-1 text-sm text-muted">{t("access.add_member_help")}</p>
        <form
          className="mt-4 grid gap-3"
          onSubmit={(event) => void onLookup(event)}
        >
          <FormField label={t("access.lookup_username")} htmlFor="lookup-username">
            <FormInput
              id="lookup-username"
              name="username"
              minLength={2}
              maxLength={80}
              pattern="[A-Za-z0-9._-]+"
              autoComplete="off"
              required
              value={lookupUsername}
              onChange={(event) => setLookupUsername(event.target.value)}
            />
          </FormField>
          <Button type="submit" disabled={lookupBusy} className="min-h-11 w-full sm:w-auto">
            {t("access.lookup_submit")}
          </Button>
        </form>

        {lookupUser ? (
          <div className="mt-4 grid gap-3 rounded-[14px] border border-line bg-canvas p-3">
            <div>
              <strong className="block text-ink">{lookupUser.display_name}</strong>
              <small className="text-muted">
                @{lookupUser.username} · {t(roleKey(lookupUser.current_vault_role))}
              </small>
            </div>
            {lookupUser.current_vault_role === "owner" ? (
              <p className="text-sm text-muted">{t("access.owner_locked")}</p>
            ) : (
              <form
                className="grid gap-3"
                onSubmit={(event) => void onGrantAccess(event)}
              >
                <FormField label={t("access.member_role")} htmlFor="member-role">
                  <FormSelect
                    id="member-role"
                    value={lookupRole}
                    onChange={(event) => setLookupRole(event.target.value)}
                  >
                    <option value="operator">{t("access.role_operator")}</option>
                    <option value="viewer">{t("access.role_viewer")}</option>
                  </FormSelect>
                </FormField>
                <Button type="submit" className="min-h-11 w-full sm:w-auto">
                  {lookupUser.current_vault_role
                    ? t("access.update_access_submit")
                    : t("access.add_member_submit")}
                </Button>
              </form>
            )}
          </div>
        ) : null}
      </Panel>
      </section>

      <section data-panel="members">
      <Panel className="p-4 sm:p-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-bold">{t("access.members_title")}</h2>
          <Button
            type="button"
            variant="secondary"
            className="min-h-11"
            disabled={membersBusy}
            onClick={() => void loadMembers({ announce: true })}
          >
            {t("access.refresh_members")}
          </Button>
        </div>
        <div className="mt-3 grid gap-2">
          {members.length === 0 ? (
            <p className="text-sm text-muted">{t("access.no_members")}</p>
          ) : (
            members.map((member) => (
              <div
                key={member.id}
                className="flex flex-col gap-2 rounded-[14px] border border-line bg-canvas p-3 sm:flex-row sm:items-center sm:justify-between"
              >
                <div>
                  <strong className="block text-ink">{member.display_name}</strong>
                  <small className="text-muted">
                    @{member.username} · {t(roleKey(member.role))}
                  </small>
                </div>
                {member.role !== "owner" ? (
                  <div className="flex flex-col gap-2 sm:flex-row">
                    <Button
                      type="button"
                      variant="secondary"
                      className="min-h-11 w-full sm:w-auto"
                      onClick={() => setTransferTarget(member)}
                    >
                      {t("access.transfer_ownership")}
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      className="min-h-11 w-full sm:w-auto"
                      onClick={() => setRemoveTarget(member)}
                    >
                      {t("access.remove_member")}
                    </Button>
                  </div>
                ) : null}
              </div>
            ))
          )}
        </div>
      </Panel>
      </section>

      <QuotasPanel
        vaultId={vaultId}
        isAdmin={isAdmin}
        onNotice={show}
      />
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
          if (onTransferred) onTransferred();
          else window.location.href = "/no-vault";
        }}
      />

      <TransferOwnershipDialog
        open={transferTarget !== null}
        member={transferTarget}
        membersSettled={membersScope.hasSettledCurrent()}
        transferScope={transferScope}
        onOpenChange={(open) => {
          if (!open) setTransferTarget(null);
        }}
        onNotice={show}
        onTransferred={() => {
          setTransferTarget(null);
          if (onTransferred) onTransferred();
          else window.location.href = "/";
        }}
      />

      <ConfirmDialog
        open={removeTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRemoveTarget(null);
        }}
        title={t("access.remove_member")}
        description={t("access.remove_member_confirm")}
        confirmLabel={t("access.remove_member")}
        cancelLabel={t("access.transfer_cancel")}
        onConfirm={() => void confirmRemove()}
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
