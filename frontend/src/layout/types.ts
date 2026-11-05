import type { VaultListItem } from "@/api/types";
export type ShellCapabilities = {
  vaultName: string; locale: string; locales: string[]; vaults: VaultListItem[]; currentVaultId?: number;
};
export type ShellNavHandlers = {
  onNewVault?: () => void; onGeneral?: () => void; onSignOut?: () => void;
  onLocaleChange?: (locale: string) => void; onVaultChange?: (vaultId: number) => void;
  onFilterChange?: (state: string) => void; onProblems?: () => void;
};
