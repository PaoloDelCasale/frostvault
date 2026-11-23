/**
 * Endpoint wrappers no SPA screen calls: they exist so the client contract
 * tests keep covering these routes.
 */
import { apiRequest, type ScanResponse, type StorageEstimateRequest, type StorageEstimateResponse } from "@/api";

export function estimateGeneralStorageCost(
  payload: StorageEstimateRequest,
): Promise<StorageEstimateResponse> {
  return apiRequest<StorageEstimateResponse>("/api/general/cost-estimates/storage", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function requestScan(): Promise<ScanResponse> {
  return apiRequest<ScanResponse>("/api/vaults/1/scan", {
    method: "POST",
  });
}
