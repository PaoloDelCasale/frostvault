import type { VaultListItem } from "@/api/types";

export type ShellCapabilities = {
  vaultName: string;
  locale: string;
  locales: string[];
  vaults: VaultListItem[];
  /** Currently selected vault; defaults to the first list entry when omitted. */
  currentVaultId?: number;
};

export type ShellNavHandlers = {
  onNewVault?: () => void;
  onManageAccess?: () => void;
  onAdministration?: () => void;
  onSignOut?: () => void;
  onLocaleChange?: (locale: string) => void;
  onVaultChange?: (vaultId: number) => void;
};
