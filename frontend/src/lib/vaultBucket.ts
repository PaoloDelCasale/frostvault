import type { VaultBucketStatus, VaultCreationCancelled } from "@/api";

type Translate = (key: string, params?: Record<string, string | number>) => string;

// While a Vault's bucket is not ready the creation page asks again this often (#735).
export const BUCKET_POLL_MS = 30_000;

/** Whole minutes until a verified bucket's Vault becomes active, at least 1. */
export function minutesLeft(bucket: Pick<VaultBucketStatus, "active_in_seconds">): number {
  return Math.max(1, Math.ceil((bucket.active_in_seconds ?? 0) / 60));
}

/** What happened to the bucket of a cancelled creation: never deleted (#735). */
export function cancelledMessage(outcome: VaultCreationCancelled, t: Translate): string {
  const key = outcome.bucket_left === "created" ? "ui.vault_create.cancelled_created" : "ui.vault_create.cancelled_possible";
  return t(key, { vault: outcome.vault_name, bucket: outcome.bucket, region: outcome.region });
}
