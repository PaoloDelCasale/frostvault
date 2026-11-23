import { useQuery } from "@tanstack/react-query";
import { fetchUploadActivity } from "./endpoints";
import { apiQueryKeys } from "./query";
import type { UploadActivitySummary } from "./types";
import { CATALOG_REFRESH_MS } from "./useCatalogEvents";

const stages = ["stability_wait", "queued", "uploading", "verifying"] as const;

/** Files still to upload and the bytes not yet sent (#711). */
export function uploadBacklog(activity: UploadActivitySummary): { files: number; bytes: number } {
  const files = stages.reduce((sum, stage) => sum + activity[stage].files, 0);
  const bytes = activity.stability_wait.bytes + activity.queued.bytes
    + Math.max(0, activity.uploading.bytes - activity.uploading.transferred_bytes);
  return { files, bytes };
}

/** The one installation-wide activity query, shared by the header and the Vault page. */
export function useUploadActivity() {
  return useQuery({
    queryKey: apiQueryKeys.uploadActivity,
    queryFn: fetchUploadActivity,
    staleTime: CATALOG_REFRESH_MS,
    refetchInterval: CATALOG_REFRESH_MS,
  });
}
