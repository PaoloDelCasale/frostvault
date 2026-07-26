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

export type LocaleUpdateResponse = {
  locale: string;
  message: string;
  message_key: string;
  messages: Record<string, string>;
};

export type FilesystemFinding = {
  path: string;
  code: string;
  message: string;
  /** Present when the backend or a scan attaches remediation advice. */
  remediation?: string | null;
};

export type FilesystemCheck = {
  code: string;
  status: string;
  message: string;
  remediation?: string | null;
};

export type FilesystemHealth = {
  ok: boolean;
  uid: number | null;
  gid: number | null;
  root?: string;
  checks: FilesystemCheck[];
  findings: FilesystemFinding[];
};

export type StatsResponse = {
  states: Record<string, number>;
  storage: {
    local_bytes: number;
    cloud_bytes: number;
  };
  active_jobs: number;
  runtime: {
    last_error?: string | null;
    filesystem?: {
      findings?: FilesystemFinding[];
    };
    [key: string]: unknown;
  };
  filesystem: FilesystemHealth | null;
  delete_enabled: boolean;
};

/** Vault File row as returned by /api/files (browse or search). */
export type VaultFileListItem = {
  type: "file";
  name: string;
  path: string;
  local_exists?: number | boolean;
  local_size?: number | null;
  local_file_type?: string | null;
  cloud_exists?: number | boolean;
  cloud_size?: number | null;
  storage_class?: string | null;
  integrity?: string | null;
  availability?: string | null;
  restore_state?: string | null;
  restore_expiry?: string | null;
  state: string;
  upload_eligible?: boolean;
  recover_eligible?: boolean;
  recoverable_version_count?: number;
  cleanup_eligible?: boolean;
};

/** Directory aggregate as returned by /api/files browse mode. */
export type DirectoryListItem = {
  type: "directory";
  name: string;
  path: string;
  item_count: number;
  total_size: number;
  local_size?: number;
  cloud_size?: number;
  state: string;
  state_counts?: Record<string, number>;
  storage_class?: string | null;
  storage_class_count?: number;
  available_actions?: Record<string, number>;
};

export type ArchiveListItem = VaultFileListItem | DirectoryListItem;

export type FilesResponse = {
  items: ArchiveListItem[];
  total: number;
  page: number;
  directory: string;
  mode: "browse" | "search" | string;
};

export type PathHistoryEntry = {
  path: string;
  valid_from?: string | null;
  valid_to?: string | null;
};

export type ArchiveVersionSummary = {
  object_key?: string | null;
  version_number?: number;
  storage_class?: string | null;
  size?: number | null;
  [key: string]: unknown;
};

/** Path History + Archive Versions for one Vault File (/api/file-history). */
export type FileHistoryResponse = {
  vault_file_id: string;
  path: string;
  path_history: PathHistoryEntry[];
  versions: ArchiveVersionSummary[];
};

export type FilesQuery = {
  q?: string;
  state?: string;
  directory?: string;
  page?: number;
  page_size?: number;
};
