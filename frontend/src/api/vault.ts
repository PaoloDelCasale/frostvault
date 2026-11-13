import { apiRequest } from "./client";
import type { ArchiveListItem, FileOperationPayload, FileOperationResponse, FileVersionsResponse, FilesResponse, OperationPolicy, RecoverEstimateResponse, JobsResponse, FileJobHistoryResponse, JobRecord, GeneralVault } from "./types";

/** An immutable request scope. Never reads or changes the Session's selection.
 * The URL, including its scope, survives CSRF/reauth retries unchanged. */
export function vaultApi(vaultId: number) {
  if (!Number.isSafeInteger(vaultId) || vaultId < 1) throw new Error("Invalid Vault identity");
  function request<T>(path: string, init?: RequestInit): Promise<T> {
    return apiRequest<T>(`${path}${path.includes("?") ? "&" : "?"}vault_id=${vaultId}`, init);
  }
  function post<T>(path: string, body: unknown) { return request<T>(path, { method: "POST", body: JSON.stringify(body) }); }
  return {
    request, post,
    files: (params: URLSearchParams) => request<FilesResponse>(`/api/files?${params}`),
    details: (path: string) => request<ArchiveListItem>(`/api/files/detail?path=${encodeURIComponent(path)}`),
    versions: (path: string) => request<FileVersionsResponse>(`/api/files/versions?path=${encodeURIComponent(path)}`),
    policy: () => request<OperationPolicy>("/api/vault/operation-policy"),
    jobs: (options: { actions?: string[]; activeOnly?: boolean; path?: string; groupsBeforeId?: number } = {}) => {
      const params = new URLSearchParams();
      options.actions?.forEach((action) => params.append("action", action));
      if (options.activeOnly) params.set("active_only", "true");
      if (options.path !== undefined) params.set("path", options.path);
      if (options.groupsBeforeId) params.set("groups_before_id", String(options.groupsBeforeId));
      return request<JobsResponse>(`/api/jobs?${params}`);
    },
    groupJobs: (groupId: string, beforeId?: number) => request<FileJobHistoryResponse>(`/api/job-groups/${encodeURIComponent(groupId)}/jobs${beforeId ? `?before_id=${beforeId}` : ""}`),
    fileJobs: (path: string, beforeId?: number) => request<FileJobHistoryResponse>(`/api/files/jobs?path=${encodeURIComponent(path)}${beforeId ? `&before_id=${beforeId}` : ""}`),
    job: (id: number) => request<JobRecord>(`/api/jobs/${id}`),
    estimate: (payload: unknown) => post<RecoverEstimateResponse>("/api/recover/estimate", payload),
    action: (action: "recover" | "free-space", payload: FileOperationPayload) => post<FileOperationResponse>(`/api/${action}`, payload),
  };
}

export type Overview = {
  vaults: { id: number; uuid: string; name: string; lifecycle: GeneralVault["lifecycle"]; monthly_estimate: number; open_problem_count: number }[];
  open_problem_count: number; month: string; monthly_estimate: number; previous_month_delta: number | null;
  currency: string; pricing_effective_at: string; email_configured: boolean; last_verified_backup: string | null;
  cloud_preflight?: { status: "unknown" | "ready" | "blocked"; remediation: string | null; next_probe_at: string | null };
};
export const fetchOverview = () => apiRequest<Overview>("/api/general");
