import { MenuSelect } from "@/components/MenuSelect";

import { shellLabel } from "./labels";
import type { ShellCapabilities, ShellNavHandlers } from "./types";

type ShellNavItemsProps = {
  capabilities: ShellCapabilities;
  handlers?: ShellNavHandlers;
  t?: (key: string) => string;
  onNavigate?: () => void;
  className?: string;
  /**
   * `primary` keeps only Vault-scoped controls (switcher + owner Manage access).
   * Secondary destinations live in the account menu.
   */
  density?: "primary" | "stacked";
};

/**
 * Vault-primary navigation: current Vault switcher and owner-only Manage access.
 * Global secondary actions (New vault, Administration, Language, Sign out) are
 * intentionally not rendered here — they belong in AccountPreferencesMenu.
 */
export function ShellNavItems({
  capabilities,
  handlers,
  t,
  onNavigate,
  className,
  density = "stacked",
}: ShellNavItemsProps) {
  const { isVaultOwner, vaults, currentVaultId } = capabilities;
  const selectedVaultId = currentVaultId ?? vaults[0]?.id ?? "";
  const compact = density === "primary";
  const actionClass =
    "min-h-11 rounded-[10px] border border-input bg-surface px-4 text-left font-bold text-ink";
  const vaultLabel = shellLabel(t, "ui.vault", "Vault");
  const manageAccessLabel = shellLabel(t, "ui.manage_access", "Manage access");

  const switcher = (
    <MenuSelect
      label={vaultLabel}
      value={String(selectedVaultId || "")}
      options={vaults.map((vault) => ({
        value: String(vault.id),
        label: vault.name,
      }))}
      onValueChange={(next) => {
        const id = Number(next);
        if (!Number.isNaN(id)) handlers?.onVaultChange?.(id);
      }}
      placement={compact ? "overlay" : "inline"}
      className={compact ? "min-w-0 max-w-[12rem]" : "w-full"}
      triggerClassName={compact ? "min-w-0 max-w-[12rem]" : undefined}
    />
  );

  return (
    <div
      className={
        className ??
        (compact
          ? "flex flex-row flex-nowrap items-center gap-2"
          : "flex flex-col gap-2")
      }
    >
      {compact ? (
        switcher
      ) : (
        <div className="flex min-h-11 flex-col justify-center gap-1 text-sm font-bold text-muted">
          <span>{vaultLabel}</span>
          {switcher}
        </div>
      )}

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
    </div>
  );
}
