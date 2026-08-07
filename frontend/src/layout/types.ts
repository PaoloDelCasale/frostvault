import type { VaultListItem, VaultRole } from "@/api/types";

export type ShellCapabilities = {
  vaultName: string;
  isVaultOwner: boolean;
  canOperate: boolean;
  isAdmin: boolean;
  locale: string;
  locales: string[];
  vaults: VaultListItem[];
  /** Currently selected vault; defaults to the first list entry when omitted. */
  currentVaultId?: number;
  /** Optional role label for tests / future UI; filtering uses the flags above. */
  role?: VaultRole;
};

export type ShellNavHandlers = {
  onNewVault?: () => void;
  onManageAccess?: () => void;
  onAdministration?: () => void;
  onSignOut?: () => void;
  onLocaleChange?: (locale: string) => void;
  onVaultChange?: (vaultId: number) => void;
};
