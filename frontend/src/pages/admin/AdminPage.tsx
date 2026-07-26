import { useCallback, useEffect, useId, useState, type FormEvent } from "react";

import type { AdminUser, AdminVault } from "@/api";
import {
  createAdminUser,
  createAdminVault,
  fetchAdminUsers,
  fetchAdminVaults,
  fetchMe,
  updateAdminUser,
} from "@/api";
import { Badge } from "@/components/Badge";
import { BottomSheet } from "@/components/BottomSheet";
import { FormField, FormInput, FormSelect } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

import { MembersDialog } from "./MembersDialog";
import { PasswordDialog } from "./PasswordDialog";

type NoticeState = {
  open: boolean;
  message: string;
  error: boolean;
};

export function AdminPage() {
  const { t, ready } = useI18n();
  const id = useId();

  const [authorized, setAuthorized] = useState<boolean | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [vaults, setVaults] = useState<AdminVault[]>([]);
  const [notice, setNotice] = useState<NoticeState>({
    open: false,
    message: "",
    error: false,
  });

  const [displayName, setDisplayName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);

  const [vaultName, setVaultName] = useState("");
  const [vaultSlug, setVaultSlug] = useState("");
  const [ownerUserId, setOwnerUserId] = useState("");
  const [vaultReason, setVaultReason] = useState("");
  const [encryptionMode, setEncryptionMode] = useState<"plain" | "crypt">(
    "plain",
  );

  const [membersVault, setMembersVault] = useState<AdminVault | null>(null);
  const [membersOpen, setMembersOpen] = useState(false);

  const [resetUserId, setResetUserId] = useState<number | null>(null);
  const [userSheetOpen, setUserSheetOpen] = useState(false);
  const [userSheetTarget, setUserSheetTarget] = useState<AdminUser | null>(
    null,
  );
  const [vaultSheetOpen, setVaultSheetOpen] = useState(false);
  const [vaultSheetTarget, setVaultSheetTarget] = useState<AdminVault | null>(
    null,
  );

  const showNotice = useCallback((message: string, error = false) => {
    setNotice({ open: true, message, error });
  }, []);

  async function loadUsers() {
    const data = await fetchAdminUsers();
    setUsers(data.items);
    const active = data.items.filter((u) => u.active);
    if (active.length && !active.some((u) => String(u.id) === ownerUserId)) {
      setOwnerUserId(String(active[0]!.id));
    }
  }

  async function loadVaults() {
    const data = await fetchAdminVaults();
    setVaults(data.items);
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const me = await fetchMe();
        if (cancelled) return;
        if (!me.is_admin) {
          setAuthorized(false);
          window.location.replace("/");
          return;
        }
        setAuthorized(true);
        await Promise.all([loadUsers(), loadVaults()]);
      } catch (error) {
        if (!cancelled) {
          showNotice(
            error instanceof Error ? error.message : String(error),
            true,
          );
          setAuthorized(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- initial load only
  }, []);

  async function handleCreateUser(event: FormEvent) {
    event.preventDefault();
    try {
      await createAdminUser({
        display_name: displayName,
        username,
        password,
        is_admin: isAdmin,
      });
      setDisplayName("");
      setUsername("");
      setPassword("");
      setIsAdmin(false);
      showNotice(t("admin.user_created"));
      await loadUsers();
    } catch (error) {
      showNotice(
        error instanceof Error ? error.message : String(error),
        true,
      );
    }
  }

  async function handleCreateVault(event: FormEvent) {
    event.preventDefault();
    try {
      await createAdminVault({
        name: vaultName,
        slug: vaultSlug,
        owner_user_id: Number(ownerUserId),
        reason: vaultReason,
        encryption_mode: encryptionMode,
      });
      setVaultName("");
      setVaultSlug("");
      setVaultReason("");
      setEncryptionMode("plain");
      showNotice(t("admin.vault_created"));
      await loadVaults();
    } catch (error) {
      showNotice(
        error instanceof Error ? error.message : String(error),
        true,
      );
    }
  }

  async function toggleUser(user: AdminUser) {
    try {
      await updateAdminUser(user.id, { active: !user.active });
      showNotice(
        user.active ? t("admin.user_deactivated") : t("admin.user_reactivated"),
      );
      await loadUsers();
    } catch (error) {
      showNotice(
        error instanceof Error ? error.message : String(error),
        true,
      );
    }
  }

  async function submitPasswordReset(newPassword: string) {
    if (resetUserId === null) return;
    await updateAdminUser(resetUserId, { password: newPassword });
    showNotice(t("admin.password_updated"));
    setResetUserId(null);
  }

  if (!ready || authorized === null) {
    return (
      <main className="mx-auto w-[min(1180px,calc(100%-2rem))] py-6">
        <p className="text-muted">{t("admin.title")}</p>
      </main>
    );
  }

  if (!authorized) {
    return null;
  }

  const activeOwners = users.filter((u) => u.active);

  return (
    <div className="min-h-svh bg-canvas text-ink">
      <main
        id="main-content"
        className="mx-auto w-[min(1180px,calc(100%-2rem))] py-6"
      >
        <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="text-xs font-extrabold tracking-[0.16em] text-green uppercase">
              {t("ui.product_name")}
            </p>
            <h1 className="text-2xl font-bold tracking-tight md:text-[27px]">
              {t("admin.heading")}
            </h1>
            <p className="mt-1 text-sm text-muted">{t("admin.subtitle")}</p>
          </div>
          <a
            href="/"
            className="inline-flex min-h-11 items-center rounded-[10px] border border-input bg-white px-4 font-bold"
          >
            {t("admin.back_to_archive")}
          </a>
        </header>

        <div className="grid gap-4 md:grid-cols-2">
          <Panel className="p-5">
            <h2 className="mb-4 text-lg font-bold">{t("admin.new_user")}</h2>
            <form className="grid gap-3" onSubmit={(e) => void handleCreateUser(e)}>
              <FormField
                label={t("admin.display_name")}
                htmlFor={`${id}-display-name`}
              >
                <FormInput
                  id={`${id}-display-name`}
                  required
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                />
              </FormField>
              <FormField label={t("admin.username")} htmlFor={`${id}-username`}>
                <FormInput
                  id={`${id}-username`}
                  required
                  pattern="[a-zA-Z0-9._-]+"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              </FormField>
              <FormField
                label={t("admin.initial_password")}
                htmlFor={`${id}-password`}
              >
                <FormInput
                  id={`${id}-password`}
                  type="password"
                  required
                  minLength={12}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </FormField>
              <label className="flex min-h-11 items-center gap-2 text-sm font-bold text-muted">
                <input
                  type="checkbox"
                  checked={isAdmin}
                  onChange={(e) => setIsAdmin(e.target.checked)}
                />
                {t("admin.is_admin")}
              </label>
              <Button type="submit" variant="primary">
                {t("admin.create_user")}
              </Button>
            </form>
          </Panel>

          <Panel className="p-5">
            <h2 className="mb-4 text-lg font-bold">{t("admin.new_vault")}</h2>
            <form
              className="grid gap-3"
              onSubmit={(e) => void handleCreateVault(e)}
            >
              <FormField
                label={t("admin.vault_name")}
                htmlFor={`${id}-vault-name`}
              >
                <FormInput
                  id={`${id}-vault-name`}
                  required
                  value={vaultName}
                  onChange={(e) => setVaultName(e.target.value)}
                />
              </FormField>
              <FormField
                label={t("admin.vault_slug")}
                htmlFor={`${id}-vault-slug`}
              >
                <FormInput
                  id={`${id}-vault-slug`}
                  required
                  pattern="[a-z0-9-]+"
                  value={vaultSlug}
                  onChange={(e) => setVaultSlug(e.target.value)}
                />
              </FormField>
              <FormField
                label={t("admin.vault_owner")}
                htmlFor={`${id}-vault-owner`}
              >
                <FormSelect
                  id={`${id}-vault-owner`}
                  required
                  value={ownerUserId}
                  onChange={(e) => setOwnerUserId(e.target.value)}
                >
                  {activeOwners.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.display_name} ({u.username})
                    </option>
                  ))}
                </FormSelect>
              </FormField>
              <FormField
                label={t("admin.encryption_mode")}
                htmlFor={`${id}-encryption`}
              >
                <FormSelect
                  id={`${id}-encryption`}
                  value={encryptionMode}
                  onChange={(e) =>
                    setEncryptionMode(e.target.value as "plain" | "crypt")
                  }
                >
                  <option value="plain">{t("admin.encryption_plain")}</option>
                  <option value="crypt">{t("admin.encryption_crypt")}</option>
                </FormSelect>
              </FormField>
              <FormField
                label={t("admin.vault_reason")}
                htmlFor={`${id}-vault-reason`}
              >
                <FormInput
                  id={`${id}-vault-reason`}
                  required
                  minLength={3}
                  maxLength={500}
                  value={vaultReason}
                  onChange={(e) => setVaultReason(e.target.value)}
                />
              </FormField>
              <Button type="submit" variant="primary">
                {t("admin.create_vault")}
              </Button>
            </form>
          </Panel>
        </div>

        <section className="mt-4">
          <Panel className="p-5">
            <h2 className="mb-4 text-lg font-bold">{t("admin.users")}</h2>
            <ul className="grid gap-2">
              {users.map((user) => (
                <li
                  key={user.id}
                  className="flex flex-wrap items-center justify-between gap-3 border-b border-line py-3"
                >
                  <div className="min-w-0">
                    <strong className="block truncate">
                      {user.display_name}
                    </strong>
                    <small className="text-muted">
                      @{user.username} ·{" "}
                      {t("admin.vaults_count", { count: user.vault_count })}
                      {user.is_admin
                        ? ` · ${t("admin.administrator")}`
                        : ""}
                    </small>
                  </div>
                    <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      state={user.active ? "both" : "missing"}
                      label={
                        user.active ? t("admin.active") : t("admin.disabled")
                      }
                    />
                    <Button
                      type="button"
                      variant="secondary"
                      className="md:hidden"
                      aria-label={t("admin.row_actions")}
                      onClick={() => {
                        setUserSheetTarget(user);
                        setUserSheetOpen(true);
                      }}
                    >
                      ⋯
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      className="max-md:hidden"
                      onClick={() => setResetUserId(user.id)}
                    >
                      {t("admin.new_password")}
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      className="max-md:hidden"
                      onClick={() => void toggleUser(user)}
                    >
                      {user.active
                        ? t("admin.deactivate")
                        : t("admin.reactivate")}
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          </Panel>
        </section>

        <section className="mt-4">
          <Panel className="p-5">
            <h2 className="mb-4 text-lg font-bold">{t("admin.vaults")}</h2>
            <ul className="grid gap-2">
              {vaults.map((vault) => (
                <li
                  key={vault.id}
                  className="flex flex-wrap items-center justify-between gap-3 border-b border-line py-3"
                >
                  <div className="min-w-0">
                    <strong className="block truncate">{vault.name}</strong>
                    <small className="text-muted">
                      {vault.slug} ·{" "}
                      {t("admin.members_count", {
                        count: vault.member_count,
                      })}{" "}
                      · {vault.source_root}
                    </small>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      state={vault.enabled ? "both" : "missing"}
                      label={
                        vault.enabled ? t("admin.active") : t("admin.disabled")
                      }
                    />
                    <Button
                      type="button"
                      variant="secondary"
                      className="md:hidden"
                      aria-label={t("admin.row_actions")}
                      onClick={() => {
                        setVaultSheetTarget(vault);
                        setVaultSheetOpen(true);
                      }}
                    >
                      ⋯
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      className="max-md:hidden"
                      onClick={() => {
                        setMembersVault(vault);
                        setMembersOpen(true);
                      }}
                    >
                      {t("admin.manage_access")}
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          </Panel>
        </section>
      </main>

      <MembersDialog
        open={membersOpen}
        onOpenChange={setMembersOpen}
        vault={membersVault}
        users={users}
        onNotice={showNotice}
        onVaultsChanged={loadVaults}
      />

      <PasswordDialog
        open={resetUserId !== null}
        onOpenChange={(open) => {
          if (!open) setResetUserId(null);
        }}
        title={t("admin.reset_password_title")}
        description={t("admin.reset_password_description")}
        submitLabel={t("admin.reset_password_submit")}
        onSubmit={submitPasswordReset}
      />

      <BottomSheet
        open={userSheetOpen}
        onOpenChange={setUserSheetOpen}
        title={userSheetTarget?.display_name ?? t("admin.row_actions")}
        actions={[
          { id: "password", label: t("admin.new_password") },
          {
            id: "toggle",
            label: userSheetTarget?.active
              ? t("admin.deactivate")
              : t("admin.reactivate"),
          },
        ]}
        onAction={(actionId) => {
          if (!userSheetTarget) return;
          if (actionId === "password") setResetUserId(userSheetTarget.id);
          if (actionId === "toggle") void toggleUser(userSheetTarget);
        }}
      />

      <BottomSheet
        open={vaultSheetOpen}
        onOpenChange={setVaultSheetOpen}
        title={vaultSheetTarget?.name ?? t("admin.row_actions")}
        actions={[
          { id: "members", label: t("admin.manage_access") },
        ]}
        onAction={(actionId) => {
          if (actionId === "members" && vaultSheetTarget) {
            setMembersVault(vaultSheetTarget);
            setMembersOpen(true);
          }
        }}
      />

      <Toast
        open={notice.open}
        message={notice.message}
        variant={notice.error ? "error" : "success"}
        onClose={() => setNotice((n) => ({ ...n, open: false }))}
      />
    </div>
  );
}
