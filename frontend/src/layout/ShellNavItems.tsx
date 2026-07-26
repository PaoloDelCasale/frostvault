import type { ShellCapabilities, ShellNavHandlers } from "./types";

type ShellNavItemsProps = {
  capabilities: ShellCapabilities;
  handlers?: ShellNavHandlers;
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

  return (
    <div className={className ?? "flex flex-col gap-2"}>
      <label className="flex min-h-11 flex-col justify-center gap-1 text-sm font-bold text-muted">
        <span>Vault</span>
        <select
          aria-label="Vault"
          className="min-h-11 rounded-[10px] border border-input bg-white px-3 text-ink"
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
        className="min-h-11 rounded-[10px] border border-input bg-white px-4 text-left font-bold text-ink"
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
          className="min-h-11 rounded-[10px] border border-input bg-white px-4 text-left font-bold text-ink"
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
          className="min-h-11 rounded-[10px] border border-input bg-white px-4 text-left font-bold text-ink"
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
          className="min-h-11 rounded-[10px] border border-input bg-white px-4 text-left font-bold text-ink"
          onClick={() => {
            handlers?.onRefreshList?.();
            onNavigate?.();
          }}
        >
          Refresh list
        </button>
      ) : null}

      <label className="flex min-h-11 flex-col justify-center gap-1 text-sm font-bold text-muted">
        <span>Language</span>
        <select
          aria-label="Language"
          className="min-h-11 rounded-[10px] border border-input bg-white px-3 text-ink"
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

      <button
        type="button"
        className="min-h-11 rounded-[10px] border border-input bg-white px-4 text-left font-bold text-ink"
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
