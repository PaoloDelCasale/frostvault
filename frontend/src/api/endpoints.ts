import { apiRequest, configureApiClient, setCsrfToken } from "./client";
import type {
  AdminMembershipCreatePayload,
  AdminOwnerTransferPayload,
  AdminUser,
  AdminUserCreatePayload,
  AdminUsersResponse,
  AdminUserUpdatePayload,
  AdminVault,
  AdminVaultCreatePayload,
  AdminVaultMembersResponse,
  AdminVaultsResponse,
  I18nCatalogResponse,
  LocaleUpdateResponse,
  MeResponse,
  RecoveryExportResponse,
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

export function fetchAdminUsers(): Promise<AdminUsersResponse> {
  return apiRequest<AdminUsersResponse>("/api/admin/users");
}

export function createAdminUser(
  payload: AdminUserCreatePayload,
): Promise<AdminUser> {
  return apiRequest<AdminUser>("/api/admin/users", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateAdminUser(
  userId: number,
  payload: AdminUserUpdatePayload,
): Promise<AdminUser> {
  return apiRequest<AdminUser>(`/api/admin/users/${userId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function fetchAdminVaults(): Promise<AdminVaultsResponse> {
  return apiRequest<AdminVaultsResponse>("/api/admin/vaults");
}

export function createAdminVault(
  payload: AdminVaultCreatePayload,
): Promise<AdminVault> {
  return apiRequest<AdminVault>("/api/admin/vaults", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function fetchAdminVaultMembers(
  vaultId: number,
): Promise<AdminVaultMembersResponse> {
  return apiRequest<AdminVaultMembersResponse>(
    `/api/admin/vaults/${vaultId}/members`,
  );
}

export function addAdminVaultMember(
  vaultId: number,
  payload: AdminMembershipCreatePayload,
): Promise<unknown> {
  return apiRequest(`/api/admin/vaults/${vaultId}/members`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function removeAdminVaultMember(
  vaultId: number,
  userId: number,
  reason: string,
): Promise<unknown> {
  const query = new URLSearchParams({ reason });
  return apiRequest(
    `/api/admin/vaults/${vaultId}/members/${userId}?${query}`,
    { method: "DELETE" },
  );
}

export function fetchAdminVaultQuotas(
  vaultId: number,
): Promise<VaultQuotasResponse> {
  return apiRequest<VaultQuotasResponse>(
    `/api/admin/vaults/${vaultId}/quotas`,
  );
}

export function updateAdminVaultQuotas(
  vaultId: number,
  payload: VaultQuotaUpdatePayload,
): Promise<VaultQuotasResponse> {
  return apiRequest<VaultQuotasResponse>(
    `/api/admin/vaults/${vaultId}/quotas`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
  );
}

export function transferAdminVaultOwner(
  vaultId: number,
  payload: AdminOwnerTransferPayload,
): Promise<unknown> {
  return apiRequest(`/api/admin/vaults/${vaultId}/transfer-owner`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function exportAdminVaultRecovery(
  vaultId: number,
  reason: string,
): Promise<RecoveryExportResponse> {
  return apiRequest<RecoveryExportResponse>(
    `/api/admin/vaults/${vaultId}/recovery/export`,
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
  );
}
