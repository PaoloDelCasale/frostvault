import { shellLabel } from "./labels";
import type { ShellCapabilities, ShellNavHandlers } from "./types";

type ShellNavItemsProps = {
  capabilities: ShellCapabilities;
  handlers?: ShellNavHandlers;
  t?: (key: string) => string;
  onNavigate?: () => void;
  className?: string;
};

const localeLabels: Record<string, { key: string; fallback: string }> = {
  en: { key: "ui.language_en", fallback: "English" },
  it: { key: "ui.language_it", fallback: "Italiano" },
};

export function ShellNavItems({
  capabilities,
  handlers,
  t,
  onNavigate,
  className,
}: ShellNavItemsProps) {
  const {
    isVaultOwner,
    canOperate,
    isAdmin,
    locale,
    locales,
    vaults,
    currentVaultId,
  } = capabilities;
  const selectedVaultId = currentVaultId ?? vaults[0]?.id ?? "";
  const actionClass =
    "min-h-11 rounded-[10px] border border-input bg-surface px-4 text-left font-bold text-ink";
  const selectClass =
    "min-h-11 rounded-[10px] border border-input bg-surface px-3 text-ink";
  const vaultLabel = shellLabel(t, "ui.vault", "Vault");
  const languageLabel = shellLabel(t, "ui.language", "Language");
  const refreshListLabel = shellLabel(t, "ui.refresh_list", "Refresh list");
  const newVaultLabel = shellLabel(t, "ui.new_vault", "New vault");
  const manageAccessLabel = shellLabel(t, "ui.manage_access", "Manage access");
  const administrationLabel = shellLabel(t, "ui.administration", "Administration");
  const signOutLabel = shellLabel(t, "ui.sign_out", "Sign out");

  return (
    <div className={className ?? "flex flex-col gap-2"}>
      <label className="flex min-h-11 flex-col justify-center gap-1 text-sm font-bold text-muted">
        <span>{vaultLabel}</span>
        <select
          aria-label={vaultLabel}
          className={selectClass}
          value={selectedVaultId}
          onChange={(event) => {
            const id = Number(event.target.value);
            if (!Number.isNaN(id)) handlers?.onVaultChange?.(id);
          }}
        >
          {vaults.map((vault) => (
            <option key={vault.id} value={vault.id}>
              {vault.name}
            </option>
          ))}
        </select>
      </label>

      <button
        type="button"
        className={actionClass}
        onClick={() => {
          handlers?.onNewVault?.();
          onNavigate?.();
        }}
      >
        {newVaultLabel}
      </button>

      {isVaultOwner ? (
        <button
          type="button"
          className={actionClass}
          onClick={() => {
            handlers?.onManageAccess?.();
            onNavigate?.();
          }}
        >
          {manageAccessLabel}
        </button>
      ) : null}

      {isAdmin ? (
        <button
          type="button"
          className={actionClass}
          onClick={() => {
            handlers?.onAdministration?.();
            onNavigate?.();
          }}
        >
          {administrationLabel}
        </button>
      ) : null}

      {canOperate ? (
        <button
          type="button"
          className={actionClass}
          onClick={() => {
            handlers?.onRefreshList?.();
            onNavigate?.();
          }}
        >
          {refreshListLabel}
        </button>
      ) : null}

      <label className="flex min-h-11 flex-col justify-center gap-1 text-sm font-bold text-muted">
        <span>{languageLabel}</span>
        <select
          aria-label={languageLabel}
          className={selectClass}
          value={locale}
          onChange={(event) => handlers?.onLocaleChange?.(event.target.value)}
        >
          {locales.map((code) => {
            const locale = localeLabels[code];
            return (
              <option key={code} value={code}>
                {locale ? shellLabel(t, locale.key, locale.fallback) : code}
              </option>
            );
          })}
        </select>
      </label>

      <button
        type="button"
        className={actionClass}
        onClick={() => {
          handlers?.onSignOut?.();
          onNavigate?.();
        }}
      >
        {signOutLabel}
      </button>
    </div>
  );
}
