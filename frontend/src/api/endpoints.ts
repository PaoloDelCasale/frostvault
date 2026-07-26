import { apiRequest, configureApiClient, setCsrfToken } from "./client";
import type {
  CloudDeletionSettings,
  GlobPreviewResponse,
  I18nCatalogResponse,
  LifecycleResponse,
  LocaleUpdateResponse,
  MeResponse,
  OperationPolicy,
  UserLookupResult,
  VaultMembersResponse,
  VaultQuotaUpdatePayload,
  VaultQuotasResponse,
  VaultsResponse,
} from "./types";
import { translate } from "@/i18n/translate";

export function fetchMe(): Promise<MeResponse> {
  return apiRequest<MeResponse>("/api/me").then((me) => {
    setCsrfToken(me.csrf_token);
    configureApiClient({ getAuthMethod: () => me.auth_method });
    return me;
  });
}

export function fetchVaults(): Promise<VaultsResponse> {
  return apiRequest<VaultsResponse>("/api/vaults");
}

export function fetchI18nCatalog(locale?: string): Promise<I18nCatalogResponse> {
  const query = locale ? `?locale=${encodeURIComponent(locale)}` : "";
  return apiRequest<I18nCatalogResponse>(`/api/i18n/catalog${query}`).then(
    (catalog) => {
      configureApiClient({
        translate: (key, params) => translate(catalog.messages, key, params),
      });
      return catalog;
    },
  );
}

export function updateLocale(locale: string): Promise<LocaleUpdateResponse> {
  return apiRequest<LocaleUpdateResponse>("/api/locale", {
    method: "PUT",
    body: JSON.stringify({ locale }),
  });
}

export function lookupVaultUser(username: string): Promise<UserLookupResult> {
  return apiRequest<UserLookupResult>("/api/vault/user-lookup", {
    method: "POST",
    body: JSON.stringify({ username }),
  });
}

export function fetchVaultMembers(): Promise<VaultMembersResponse> {
  return apiRequest<VaultMembersResponse>("/api/vault/members");
}

export function addVaultMember(
  userId: number,
  role: string,
): Promise<{ message: string }> {
  return apiRequest<{ message: string }>("/api/vault/members", {
    method: "POST",
    body: JSON.stringify({ user_id: userId, role }),
  });
}

export function removeVaultMember(userId: number): Promise<{ message: string }> {
  return apiRequest<{ message: string }>(`/api/vault/members/${userId}`, {
    method: "DELETE",
  });
}

export function transferVaultOwner(
  newOwnerUserId: number,
): Promise<{ message: string }> {
  return apiRequest<{ message: string }>("/api/vault/transfer-owner", {
    method: "POST",
    body: JSON.stringify({ new_owner_user_id: newOwnerUserId }),
  });
}

export function fetchVaultQuotas(): Promise<VaultQuotasResponse> {
  return apiRequest<VaultQuotasResponse>("/api/vault/quotas");
}

export function updateAdminVaultQuotas(
  vaultId: number,
  payload: VaultQuotaUpdatePayload,
): Promise<VaultQuotasResponse> {
  return apiRequest<VaultQuotasResponse>(`/api/admin/vaults/${vaultId}/quotas`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function fetchOperationPolicy(): Promise<OperationPolicy> {
  return apiRequest<OperationPolicy>("/api/vault/operation-policy");
}

export function updateOperationPolicy(
  policy: OperationPolicy,
): Promise<OperationPolicy> {
  return apiRequest<OperationPolicy>("/api/vault/operation-policy", {
    method: "PUT",
    body: JSON.stringify(policy),
  });
}

export function previewOperationGlobs(payload: {
  paths: string[];
  include_globs: string[];
  exclude_globs: string[];
}): Promise<GlobPreviewResponse> {
  return apiRequest<GlobPreviewResponse>(
    "/api/vault/operation-policy/preview-globs",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function fetchLifecycle(): Promise<LifecycleResponse> {
  return apiRequest<LifecycleResponse>("/api/vault/lifecycle");
}

export function updateLifecycleDefault(
  guidedProfile: string,
): Promise<LifecycleResponse> {
  return apiRequest<LifecycleResponse>("/api/vault/lifecycle/default", {
    method: "PUT",
    body: JSON.stringify({ guided_profile: guidedProfile }),
  });
}

export function upsertLifecycleFolderOverride(
  folderPath: string,
  guidedProfile: string,
): Promise<LifecycleResponse> {
  return apiRequest<LifecycleResponse>("/api/vault/lifecycle/folder-overrides", {
    method: "PUT",
    body: JSON.stringify({
      folder_path: folderPath,
      guided_profile: guidedProfile,
    }),
  });
}

export function deleteLifecycleFolderOverride(
  folderPath: string,
): Promise<LifecycleResponse> {
  return apiRequest<LifecycleResponse>("/api/vault/lifecycle/folder-overrides", {
    method: "DELETE",
    body: JSON.stringify({ folder_path: folderPath }),
  });
}

export function fetchCloudDeletion(): Promise<CloudDeletionSettings> {
  return apiRequest<CloudDeletionSettings>("/api/vault/cloud-deletion");
}

export function updateCloudDeletion(
  enabled: boolean,
): Promise<CloudDeletionSettings> {
  return apiRequest<CloudDeletionSettings>("/api/vault/cloud-deletion", {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  });
}
