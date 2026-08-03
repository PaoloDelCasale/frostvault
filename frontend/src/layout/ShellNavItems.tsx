import { ThemeControl } from "@/components/ThemeControl";

import type { ShellCapabilities, ShellNavHandlers } from "./types";

type ShellNavItemsProps = {
  capabilities: ShellCapabilities;
  handlers?: ShellNavHandlers;
  t?: (key: string) => string;
  onNavigate?: () => void;
  className?: string;
};

const localeLabel: Record<string, string> = {
  en: "English",
  it: "Italiano",
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
  const refreshListLabel = t?.("ui.refresh_list") ?? "Refresh list";

  return (
    <div className={className ?? "flex flex-col gap-2"}>
      <label className="flex min-h-11 flex-col justify-center gap-1 text-sm font-bold text-muted">
        <span>Vault</span>
        <select
          aria-label="Vault"
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
        New vault
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
          Manage access
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
          Administration
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
        <span>Language</span>
        <select
          aria-label="Language"
          className={selectClass}
          value={locale}
          onChange={(event) => handlers?.onLocaleChange?.(event.target.value)}
        >
          {locales.map((code) => (
            <option key={code} value={code}>
              {localeLabel[code] ?? code}
            </option>
          ))}
        </select>
      </label>

      <ThemeControl />

      <button
        type="button"
        className={actionClass}
        onClick={() => {
          handlers?.onSignOut?.();
          onNavigate?.();
        }}
      >
        Sign out
      </button>
    </div>
  );
}
