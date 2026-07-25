/** Hand-written response types for foundation endpoints (issue #60). */

export type VaultRole = "owner" | "operator" | "viewer";

export type AuthMethod = "oidc" | "local" | string | null;

export type MeVault = {
  id: number;
  slug: string;
  name: string;
  role: VaultRole;
  can_operate: boolean;
  delete_enabled: boolean;
  cloud_deletion_enabled: boolean;
  is_vault_owner: boolean;
};

export type MeResponse = {
  id: number;
  username: string;
  display_name: string;
  is_admin: boolean;
  active: boolean;
  session_version: number;
  csrf_token: string;
  auth_method: AuthMethod;
  locale: string;
  locales: string[];
  vault: MeVault | null;
};

export type VaultListItem = {
  id: number;
  slug: string;
  name: string;
  role: VaultRole;
};

export type VaultsResponse = {
  items: VaultListItem[];
};

export type I18nCatalogResponse = {
  locale: string;
  locales: string[];
  messages: Record<string, string>;
};
