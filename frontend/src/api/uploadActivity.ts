import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "@/api/client";
import type { UploadActivityResponse, UploadActivitySummary } from "@/api/types";
import { CATALOG_REFRESH_MS } from "@/api/useCatalogEvents";
import { formatRemaining, minutesLeft } from "@/lib/remaining";
import { formatBytes } from "@/pages/archive/format";

type Translate = (key: string, params?: Record<string, string | number>) => string;
const stages = ["stability_wait", "queued", "uploading", "verifying"] as const;

/** Files still to upload and the bytes not yet sent (#711). */
export function uploadBacklog(activity: UploadActivitySummary): { files: number; bytes: number } {
  const files = stages.reduce((sum, stage) => sum + activity[stage].files, 0);
  const bytes = activity.stability_wait.bytes + activity.queued.bytes
    + Math.max(0, activity.uploading.bytes - activity.uploading.transferred_bytes);
  return { files, bytes };
}

/** The measured pace and the time left at it, or null before it is measured (#711). */
export function uploadPace(activity: UploadActivitySummary, t: Translate): { rate: string; remaining: string } | null {
  const perMinute = activity.rate?.bytes_per_minute;
  const minutes = minutesLeft(uploadBacklog(activity).bytes, perMinute);
  return minutes === null ? null : { rate: formatBytes(Number(perMinute)), remaining: formatRemaining(minutes, t) };
}

/** The one installation-wide activity query, shared by the header and the Vault page. */
export function useUploadActivity() {
  return useQuery({
    queryKey: ["upload-activity"],
    queryFn: () => apiRequest<UploadActivityResponse>("/api/upload-activity"),
    staleTime: CATALOG_REFRESH_MS,
    refetchInterval: CATALOG_REFRESH_MS,
  });
}
