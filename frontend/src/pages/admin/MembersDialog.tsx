import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import type {
  AdminUser,
  AdminVault,
  AdminVaultMember,
  VaultQuotasResponse,
} from "@/api";
import {
  addAdminVaultMember,
  createLatestRequestScope,
  exportAdminVaultRecovery,
  fetchAdminVaultMembers,
  fetchAdminVaultQuotas,
  removeAdminVaultMember,
  transferAdminVaultOwner,
  updateAdminVaultQuotas,
} from "@/api";
import { BottomSheet } from "@/components/BottomSheet";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput, FormSelect } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

import {
  buildQuotaUpdatePayload,
  emptyQuotaFormValues,
  formatQuotaValue,
  limitsToFormValues,
  quotaStatusItems,
  type QuotaFormValues,
} from "./quota";

type MembersDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: AdminVault | null;
  users: AdminUser[];
  onNotice: (message: string, error?: boolean) => void;
  onVaultsChanged: () => Promise<void>;
};

export function MembersDialog({
  open,
  onOpenChange,
  vault,
  users,
  onNotice,
  onVaultsChanged,
}: MembersDialogProps) {
  const { t } = useI18n();
  const id = useId();

  const selectionScope = useRef(createLatestRequestScope());
  const membersScope = useRef(createLatestRequestScope());
  const quotasScope = useRef(createLatestRequestScope());
  const quotaSaveScope = useRef(createLatestRequestScope());
  const membershipScope = useRef(createLatestRequestScope());
  const transferScope = useRef(createLatestRequestScope());

  const [members, setMembers] = useState<AdminVaultMember[]>([]);
  const [membersLoaded, setMembersLoaded] = useState(false);
  const [quotaForm, setQuotaForm] = useState<QuotaFormValues>(
    emptyQuotaFormValues(),
  );
  const [quotaLoaded, setQuotaLoaded] = useState(false);
  const [quotaLoadState, setQuotaLoadState] = useState("");
  const [quotaUsage, setQuotaUsage] = useState<VaultQuotasResponse | null>(
    null,
  );
  const [quotaSaving, setQuotaSaving] = useState(false);
  const [transferBusy, setTransferBusy] = useState(false);
  const [transferUserId, setTransferUserId] = useState("");
  const [transferReason, setTransferReason] = useState("");
  const [transferConfirm, setTransferConfirm] = useState("");
  const [memberUserId, setMemberUserId] = useState("");
  const [memberRole, setMemberRole] = useState<"operator" | "viewer">(
    "operator",
  );
  const [memberReason, setMemberReason] = useState("");
  const [recoveryReason, setRecoveryReason] = useState("");
  const [recoveryExport, setRecoveryExport] = useState("");
  const [memberActionsOpen, setMemberActionsOpen] = useState(false);
  const [memberActionsTarget, setMemberActionsTarget] =
    useState<AdminVaultMember | null>(null);
  const [removeConfirmOpen, setRemoveConfirmOpen] = useState(false);
  const [removeReason, setRemoveReason] = useState("");

  const vaultId = vault?.id ?? null;
  const vaultName = vault?.name ?? "";

  useEffect(() => {
    if (!open || vaultId === null || !vault) return;

    selectionScope.current.begin();
    const selectionToken = selectionScope.current.current;
    const isSelected = () =>
      selectionScope.current.current === selectionToken;

    setMembers([]);
    setMembersLoaded(false);
    setQuotaForm(emptyQuotaFormValues());
    setQuotaLoaded(false);
    setQuotaUsage(null);
    setQuotaLoadState(t("admin.quota_loading"));
    setTransferUserId("");
    setTransferReason("");
    setTransferConfirm("");
    setRecoveryExport("");
    setRecoveryReason("");
    setQuotaSaving(false);
    // Invalidate in-flight saves/transfers when selection changes.
    quotaSaveScope.current.begin();
    transferScope.current.begin();
    membershipScope.current.begin();

    const membersHandle = membersScope.current.begin();
    void membersHandle
      .settle(fetchAdminVaultMembers(vaultId))
      .then((data) => {
        if (!data || !isSelected()) return;
        setMembers(data.items);
        setMembersLoaded(true);
        const eligible = data.items.filter(
          (m) => m.active && m.role !== "owner",
        );
        setTransferUserId(
          eligible.length ? String(eligible[0]!.id) : "",
        );
      })
      .catch((error: unknown) => {
        if (!isSelected()) return;
        onNotice(
          error instanceof Error ? error.message : String(error),
          true,
        );
      });

    const quotasHandle = quotasScope.current.begin();
    void quotasHandle
      .settle(fetchAdminVaultQuotas(vaultId))
      .then((data) => {
        if (!data || !isSelected()) return;
        setQuotaForm(limitsToFormValues(data.limits));
        setQuotaUsage(data);
        setQuotaLoaded(true);
        setQuotaLoadState(t("admin.quota_loaded"));
      })
      .catch((error: unknown) => {
        if (!isSelected()) return;
        setQuotaLoadState(
          error instanceof Error ? error.message : String(error),
        );
      });
    // Intentionally depend on vault identity only — onNotice/t are stable enough
    // via refs below; recreating on every parent render would wipe in-progress forms.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, vaultId, vault?.id, vault?.name]);

  const eligibleTransferTargets = members.filter(
    (m) => m.active && m.role !== "owner",
  );
  const activeUsers = users.filter((u) => u.active);

  async function refreshMembers() {
    if (vaultId === null) return;
    const selectionToken = selectionScope.current.current;
    const handle = membersScope.current.begin();
    setMembersLoaded(false);
    const data = await handle.settle(fetchAdminVaultMembers(vaultId));
    if (!data || selectionScope.current.current !== selectionToken) return;
    setMembers(data.items);
    setMembersLoaded(true);
    const eligible = data.items.filter(
      (m) => m.active && m.role !== "owner",
    );
    setTransferUserId(eligible.length ? String(eligible[0]!.id) : "");
  }

  async function handleQuotaSave(event: FormEvent) {
    event.preventDefault();
    if (vaultId === null || !quotaLoaded) {
      onNotice(t("admin.quota_wait"), true);
      return;
    }
    const built = buildQuotaUpdatePayload(quotaForm);
    if (!built.ok) {
      onNotice(built.error, true);
      return;
    }
    const selectionToken = selectionScope.current.current;
    const handle = quotaSaveScope.current.begin();
    const isCurrent = () =>
      handle.isCurrent() &&
      selectionScope.current.current === selectionToken;
    setQuotaSaving(true);
    try {
      const data = await handle.settle(
        updateAdminVaultQuotas(vaultId, built.payload),
      );
      if (!data || !isCurrent()) return;
      setQuotaForm({
        ...limitsToFormValues(data.limits),
        reason: "",
      });
      setQuotaUsage(data);
      setQuotaLoaded(true);
      setQuotaLoadState(t("admin.quota_loaded"));
      onNotice(t("admin.quota_updated"));
    } catch (error) {
      if (isCurrent()) {
        onNotice(
          error instanceof Error ? error.message : String(error),
          true,
        );
      }
    } finally {
      if (isCurrent()) setQuotaSaving(false);
    }
  }

  async function handleAssignMember(event: FormEvent) {
    event.preventDefault();
    if (vaultId === null) return;
    const reason = memberReason.trim();
    if (!reason) {
      onNotice(t("admin.member_reason_required"), true);
      return;
    }
    const selectionToken = selectionScope.current.current;
    const handle = membershipScope.current.begin();
    const isCurrent = () =>
      handle.isCurrent() &&
      selectionScope.current.current === selectionToken;
    try {
      await handle.settle(
        addAdminVaultMember(vaultId, {
          user_id: Number(memberUserId),
          role: memberRole,
          reason,
        }),
      );
      if (!isCurrent()) return;
      onNotice(t("admin.access_updated"));
      setMemberReason("");
      await refreshMembers();
      if (!isCurrent()) return;
      await onVaultsChanged();
    } catch (error) {
      if (isCurrent()) {
        onNotice(
          error instanceof Error ? error.message : String(error),
          true,
        );
      }
    }
  }

  async function handleTransfer(event: FormEvent) {
    event.preventDefault();
    if (vaultId === null || !membersLoaded) {
      onNotice(t("admin.transfer_wait"), true);
      return;
    }
    const reason = transferReason.trim();
    if (!reason) {
      onNotice(t("admin.transfer_reason_required"), true);
      return;
    }
    if (transferConfirm.trim() !== vaultName) {
      onNotice(t("admin.transfer_confirm_mismatch"), true);
      return;
    }
    const selectionToken = selectionScope.current.current;
    const handle = transferScope.current.begin();
    const isCurrent = () =>
      handle.isCurrent() &&
      selectionScope.current.current === selectionToken;
    setTransferBusy(true);
    try {
      await handle.settle(
        transferAdminVaultOwner(vaultId, {
          new_owner_user_id: Number(transferUserId),
          reason,
        }),
      );
      if (!isCurrent()) return;
      onNotice(t("admin.ownership_transferred"));
      setTransferReason("");
      setTransferConfirm("");
      await refreshMembers();
      if (!isCurrent()) return;
      await onVaultsChanged();
    } catch (error) {
      if (isCurrent()) {
        onNotice(
          error instanceof Error ? error.message : String(error),
          true,
        );
      }
    } finally {
      if (isCurrent()) setTransferBusy(false);
    }
  }

  async function confirmRemoveMember() {
    if (vaultId === null || !memberActionsTarget) return;
    const reason = removeReason.trim();
    if (!reason) return;
    const selectionToken = selectionScope.current.current;
    const handle = membershipScope.current.begin();
    const isCurrent = () =>
      handle.isCurrent() &&
      selectionScope.current.current === selectionToken;
    try {
      await handle.settle(
        removeAdminVaultMember(
          vaultId,
          memberActionsTarget.id,
          reason,
        ),
      );
      if (!isCurrent()) return;
      onNotice(t("admin.access_removed"));
      setRemoveConfirmOpen(false);
      setRemoveReason("");
      setMemberActionsTarget(null);
      await onVaultsChanged();
      if (!isCurrent()) return;
      await refreshMembers();
    } catch (error) {
      if (isCurrent()) {
        onNotice(
          error instanceof Error ? error.message : String(error),
          true,
        );
      }
    }
  }

  async function handleRecoveryExport(event: FormEvent) {
    event.preventDefault();
    if (vaultId === null) return;
    const reason = recoveryReason.trim();
    if (reason.length < 3) return;
    try {
      const data = await exportAdminVaultRecovery(vaultId, reason);
      setRecoveryExport(data.recovery_export);
    } catch (error) {
      onNotice(
        error instanceof Error ? error.message : String(error),
        true,
      );
    }
  }

  async function copyRecovery() {
    if (!recoveryExport) return;
    await navigator.clipboard.writeText(recoveryExport);
    onNotice(t("admin.recovery_copied"));
  }

  function downloadRecovery() {
    if (!recoveryExport || !vault) return;
    const blob = new Blob([recoveryExport], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${vault.slug}-recovery.txt`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  const statusItems = quotaStatusItems(quotaUsage?.evaluation);

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={onOpenChange}
        title={t("admin.members_title", { name: vaultName })}
        className={
          "max-md:!inset-0 max-md:!top-0 max-md:!left-0 max-md:!right-0 max-md:!bottom-0 " +
          "max-md:!h-dvh max-md:!w-full max-md:!max-w-none " +
          "max-md:!translate-x-0 max-md:!translate-y-0 max-md:!rounded-none " +
          "max-md:overflow-y-auto max-md:!p-4 " +
          "md:max-h-[min(90vh,900px)] md:overflow-y-auto"
        }
      >
        <div className="grid gap-8">
          <section aria-labelledby={`${id}-members`}>
            <h3 id={`${id}-members`} className="mb-3 text-base font-bold">
              {t("admin.members_section")}
            </h3>
            <ul className="grid gap-2">
              {members.length === 0 ? (
                <li className="text-sm text-muted">{t("admin.members_empty")}</li>
              ) : (
                members.map((member) => (
                  <li
                    key={member.id}
                    className="flex items-center justify-between gap-3 border-b border-line py-3"
                  >
                    <div className="min-w-0">
                      <strong className="block truncate">
                        {member.display_name}
                      </strong>
                      <small className="text-muted">
                        @{member.username} ·{" "}
                        {member.role === "owner"
                          ? t("admin.primary_owner")
                          : member.role}
                      </small>
                    </div>
                    {member.role !== "owner" ? (
                      <Button
                        type="button"
                        variant="secondary"
                        className="md:hidden"
                        aria-label={t("admin.row_actions")}
                        onClick={() => {
                          setMemberActionsTarget(member);
                          setMemberActionsOpen(true);
                        }}
                      >
                        ⋯
                      </Button>
                    ) : null}
                    {member.role !== "owner" ? (
                      <Button
                        type="button"
                        variant="secondary"
                        className="max-md:hidden"
                        onClick={() => {
                          setMemberActionsTarget(member);
                          setRemoveReason("");
                          setRemoveConfirmOpen(true);
                        }}
                      >
                        {t("admin.remove_member")}
                      </Button>
                    ) : null}
                  </li>
                ))
              )}
            </ul>
          </section>

          <section aria-labelledby={`${id}-quotas`} className="border-t border-line pt-6">
            <h3 id={`${id}-quotas`} className="mb-2 text-base font-bold">
              {t("admin.quotas")}
            </h3>
            <p className="mb-3 text-sm text-muted">{t("admin.quotas_help")}</p>
            <p className="mb-3 text-sm text-muted" role="status">
              {quotaLoadState || t("admin.quota_select_vault")}
            </p>
            <form className="grid gap-4" onSubmit={(e) => void handleQuotaSave(e)}>
              <div className="grid gap-3 md:grid-cols-2">
                {(
                  [
                    ["storage_soft_limit_bytes", "admin.quota_storage_soft"],
                    ["storage_hard_limit_bytes", "admin.quota_storage_hard"],
                    ["concurrency_soft_limit", "admin.quota_concurrency_soft"],
                    ["concurrency_hard_limit", "admin.quota_concurrency_hard"],
                    [
                      "restore_30d_soft_limit_bytes",
                      "admin.quota_restore_soft",
                    ],
                    [
                      "restore_30d_hard_limit_bytes",
                      "admin.quota_restore_hard",
                    ],
                  ] as const
                ).map(([name, labelKey]) => (
                  <FormField
                    key={name}
                    label={t(labelKey)}
                    htmlFor={`${id}-${name}`}
                  >
                    <FormInput
                      id={`${id}-${name}`}
                      name={name}
                      type="number"
                      min={0}
                      step={1}
                      inputMode="numeric"
                      value={quotaForm[name]}
                      onChange={(event) =>
                        setQuotaForm((prev) => ({
                          ...prev,
                          [name]: event.target.value,
                        }))
                      }
                    />
                  </FormField>
                ))}
              </div>
              <FormField
                label={t("admin.quota_reason")}
                htmlFor={`${id}-quota-reason`}
              >
                <FormInput
                  id={`${id}-quota-reason`}
                  name="reason"
                  minLength={3}
                  maxLength={500}
                  required
                  value={quotaForm.reason}
                  onChange={(event) =>
                    setQuotaForm((prev) => ({
                      ...prev,
                      reason: event.target.value,
                    }))
                  }
                />
              </FormField>
              <Button
                type="submit"
                variant="primary"
                disabled={!quotaLoaded || quotaSaving}
              >
                {t("admin.quota_save")}
              </Button>
            </form>
            {quotaUsage ? (
              <div className="mt-4 flex flex-wrap gap-x-[18px] gap-y-2 text-[13px] text-muted">
                <span>
                  {t("admin.quota_usage_storage")}:{" "}
                  <strong className="text-ink">
                    {formatQuotaValue(
                      quotaUsage.usage.storage_bytes,
                      "bytes",
                    )}
                  </strong>
                </span>
                <span>
                  {t("admin.quota_usage_jobs")}:{" "}
                  <strong className="text-ink">
                    {formatQuotaValue(
                      quotaUsage.usage.concurrency,
                      "jobs",
                    )}
                  </strong>
                </span>
                <span>
                  {t("admin.quota_usage_restore")}:{" "}
                  <strong className="text-ink">
                    {formatQuotaValue(
                      quotaUsage.usage.restore_30d_bytes,
                      "bytes",
                    )}
                  </strong>
                </span>
                {statusItems.map((item) => (
                  <span
                    key={item.label}
                    className={
                      item.kind === "ok"
                        ? "rounded-badge bg-green-soft px-2.5 py-1.5 font-bold text-[#185a37]"
                        : item.kind === "warning"
                          ? "rounded-badge bg-amber-soft px-2.5 py-1.5 font-bold text-[#775400]"
                          : item.kind === "block"
                            ? "rounded-badge bg-red-soft px-2.5 py-1.5 font-bold text-[#92372f]"
                            : "rounded-badge bg-[#eee] px-2.5 py-1.5 font-bold text-[#555]"
                    }
                  >
                    {item.label}
                  </span>
                ))}
              </div>
            ) : null}
          </section>

          <section aria-labelledby={`${id}-assign`} className="border-t border-line pt-6">
            <h3 id={`${id}-assign`} className="mb-2 text-base font-bold">
              {t("admin.assign_member")}
            </h3>
            <p className="mb-3 text-sm text-muted">{t("admin.assign_help")}</p>
            <form
              className="grid gap-3 md:grid-cols-[1fr_160px_1fr_auto]"
              onSubmit={(e) => void handleAssignMember(e)}
            >
              <FormField label={t("admin.member_user")} htmlFor={`${id}-member-user`}>
                <FormSelect
                  id={`${id}-member-user`}
                  required
                  value={memberUserId || String(activeUsers[0]?.id ?? "")}
                  onChange={(event) => setMemberUserId(event.target.value)}
                >
                  {activeUsers.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.display_name} (@{u.username})
                    </option>
                  ))}
                </FormSelect>
              </FormField>
              <FormField label={t("admin.member_role")} htmlFor={`${id}-member-role`}>
                <FormSelect
                  id={`${id}-member-role`}
                  required
                  value={memberRole}
                  onChange={(event) =>
                    setMemberRole(event.target.value as "operator" | "viewer")
                  }
                >
                  <option value="operator">{t("admin.role_operator")}</option>
                  <option value="viewer">{t("admin.role_viewer")}</option>
                </FormSelect>
              </FormField>
              <FormField
                label={t("admin.member_reason")}
                htmlFor={`${id}-member-reason`}
              >
                <FormInput
                  id={`${id}-member-reason`}
                  required
                  minLength={3}
                  maxLength={500}
                  value={memberReason}
                  onChange={(event) => setMemberReason(event.target.value)}
                />
              </FormField>
              <div className="flex items-end">
                <Button type="submit" variant="primary" className="w-full md:w-auto">
                  {t("admin.assign_access")}
                </Button>
              </div>
            </form>
          </section>

          <section aria-labelledby={`${id}-transfer`} className="border-t border-line pt-6">
            <h3 id={`${id}-transfer`} className="mb-2 text-base font-bold">
              {t("admin.transfer_title")}
            </h3>
            <p className="mb-3 text-sm text-muted">{t("admin.transfer_help")}</p>
            <form className="grid gap-3" onSubmit={(e) => void handleTransfer(e)}>
              <FormField
                label={t("admin.transfer_user")}
                htmlFor={`${id}-transfer-user`}
              >
                <FormSelect
                  id={`${id}-transfer-user`}
                  required
                  disabled={eligibleTransferTargets.length === 0 || !membersLoaded}
                  value={transferUserId}
                  onChange={(event) => setTransferUserId(event.target.value)}
                >
                  {eligibleTransferTargets.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.display_name} (@{m.username})
                    </option>
                  ))}
                </FormSelect>
              </FormField>
              <FormField
                label={t("admin.transfer_reason")}
                htmlFor={`${id}-transfer-reason`}
              >
                <FormInput
                  id={`${id}-transfer-reason`}
                  required
                  minLength={3}
                  maxLength={500}
                  value={transferReason}
                  onChange={(event) => setTransferReason(event.target.value)}
                />
              </FormField>
              <FormField
                label={t("admin.transfer_confirm_label")}
                htmlFor={`${id}-transfer-confirm`}
              >
                <FormInput
                  id={`${id}-transfer-confirm`}
                  required
                  value={transferConfirm}
                  onChange={(event) => setTransferConfirm(event.target.value)}
                  placeholder={vaultName}
                />
              </FormField>
              <Button
                type="submit"
                variant="danger"
                disabled={
                  transferBusy ||
                  (membersLoaded && eligibleTransferTargets.length === 0)
                }
              >
                {t("admin.transfer_submit")}
              </Button>
            </form>
          </section>

          {vault?.encryption_mode === "crypt" ? (
            <section
              aria-labelledby={`${id}-recovery`}
              className="border-t border-line pt-6"
            >
              <h3 id={`${id}-recovery`} className="mb-2 text-base font-bold">
                {t("admin.recovery_export")}
              </h3>
              <p className="mb-3 text-sm text-muted">{t("admin.recovery_help")}</p>
              <form
                className="grid gap-3"
                onSubmit={(e) => void handleRecoveryExport(e)}
              >
                <FormField
                  label={t("admin.recovery_reason")}
                  htmlFor={`${id}-recovery-reason`}
                >
                  <FormInput
                    id={`${id}-recovery-reason`}
                    required
                    minLength={3}
                    maxLength={500}
                    value={recoveryReason}
                    onChange={(event) =>
                      setRecoveryReason(event.target.value)
                    }
                  />
                </FormField>
                <Button type="submit" variant="secondary">
                  {t("admin.recovery_export_submit")}
                </Button>
              </form>
              {recoveryExport ? (
                <div className="mt-4 grid gap-3">
                  <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-[10px] border border-line bg-canvas p-3 text-xs">
                    {recoveryExport}
                  </pre>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={() => void copyRecovery()}
                    >
                      {t("admin.recovery_copy")}
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={downloadRecovery}
                    >
                      {t("admin.recovery_download")}
                    </Button>
                  </div>
                </div>
              ) : null}
            </section>
          ) : null}
        </div>
      </Dialog>

      <BottomSheet
        open={memberActionsOpen}
        onOpenChange={setMemberActionsOpen}
        title={memberActionsTarget?.display_name ?? t("admin.row_actions")}
        actions={[
          {
            id: "remove",
            label: t("admin.remove_member"),
            tone: "danger",
          },
        ]}
        onAction={(actionId) => {
          if (actionId === "remove") {
            setRemoveReason("");
            setRemoveConfirmOpen(true);
          }
        }}
      />

      <Dialog
        open={removeConfirmOpen}
        onOpenChange={setRemoveConfirmOpen}
        title={t("admin.remove_member_confirm_title")}
        description={t("admin.remove_member_confirm")}
        className="w-[min(28rem,calc(100%-1.75rem))]"
      >
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            void confirmRemoveMember();
          }}
        >
          <FormField
            label={t("admin.remove_member_reason")}
            htmlFor={`${id}-remove-reason`}
          >
            <FormInput
              id={`${id}-remove-reason`}
              required
              minLength={3}
              maxLength={500}
              value={removeReason}
              onChange={(event) => setRemoveReason(event.target.value)}
            />
          </FormField>
          <div className="flex flex-wrap justify-end gap-2">
            <Button
              type="button"
              variant="secondary"
              onClick={() => setRemoveConfirmOpen(false)}
            >
              {t("admin.cancel")}
            </Button>
            <Button type="submit" variant="danger">
              {t("admin.remove_member")}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
