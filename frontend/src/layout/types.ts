import type { VaultListItem } from "@/api/types";

/** The sidebar destination a page belongs to. */
export type ShellSection = "overview" | "files" | "settings" | "problems" | "system";

export type ShellCapabilities = {
  locale: string; locales: string[]; vaults: VaultListItem[]; currentVaultId?: number;
  /** A page about every Vault (Overview, Problems, System): the switcher shows «All Vaults», unchecked. */
  allVaults?: boolean;
};
export type ShellNavHandlers = {
  onSignOut?: () => void;
  onLocaleChange?: (locale: string) => void; onNavigate?: (url: string) => void;
};
