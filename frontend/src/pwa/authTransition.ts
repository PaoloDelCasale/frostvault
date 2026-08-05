import { fetchMe } from "@/api/endpoints";
import type { MeResponse } from "@/api/types";

import {
  beginOfflineFileCacheTransition,
  finishOfflineFileCacheTransition,
  isOfflineCacheContext,
  offlineFileCacheContextNeedsTransition,
  offlineFileCacheFreshnessNeedsTransition,
  offlineFileCacheTransitionWasLost,
  prepareOfflineFileCacheContext,
  setOfflineFileCacheContext,
  type OfflineCacheContext,
  type OfflineFileCacheLease,
  type OfflineFileCacheTransition,
} from "./offlineFiles";

/** Server mutations and reconciliation must not leave a page indefinitely busy. */
export const AUTH_TRANSITION_TIMEOUT_MS = 5_000;
const MAX_RECONCILIATION_ATTEMPTS = 4;

export class AuthTransitionTimeoutError extends Error {
  constructor() {
    super("Authentication transition request timed out");
    this.name = "AuthTransitionTimeoutError";
  }
}

export function withinAuthTransitionTimeout<T>(work: Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      callback();
    };
    const timeout = setTimeout(
      () => finish(() => reject(new AuthTransitionTimeoutError())),
      AUTH_TRANSITION_TIMEOUT_MS,
    );
    void work.then(
      (value) => finish(() => resolve(value)),
      (error: unknown) => finish(() => reject(error)),
    );
  });
}

/** Build the cache scope only from fresh server authority. */
export function offlineCacheContextForMe(
  me: MeResponse,
): OfflineCacheContext | null {
  if (!me.vault) return null;
  const context = {
    userId: me.id,
    vaultId: me.vault.id,
    authorizationGeneration: me.offline_cache_generation,
  };
  return isOfflineCacheContext(context) ? context : null;
}

export type OfflineAuthReconciliation = Readonly<{
  me: MeResponse;
  context: OfflineCacheContext | null;
  lease: OfflineFileCacheLease | null;
  transition: OfflineFileCacheTransition | undefined;
  workerAvailable: boolean;
}>;

export type ReconcileOfflineAuthTransitionOptions = Readonly<{
  transition?: OfflineFileCacheTransition;
  fetchAuthority?: () => Promise<MeResponse>;
}>;

/**
 * Begin the durable local close and wait only for the bounded Worker message.
 * ``workerAcknowledged`` is intentionally exposed on the returned capability:
 * it is not evidence that another page was closed when the Worker is absent.
 */
export function beginOfflineAuthTransition(): Promise<OfflineFileCacheTransition> {
  return beginOfflineFileCacheTransition();
}

/**
 * Reconcile a post-mutation Session/Vault with the Worker in one place.
 *
 * Every lease follows a bounded Worker boot handshake and a fresh /api/me. A
 * Worker restart loses any old transition capability, so this helper begins a
 * new explicit close and retries rather than letting the lost transition reopen
 * caches. If no Worker replies, it still returns authoritative server state
 * network-only while the durable local barrier remains closed.
 */
export async function reconcileOfflineAuthTransition(
  options: ReconcileOfflineAuthTransitionOptions = {},
): Promise<OfflineAuthReconciliation> {
  const fetchAuthority = options.fetchAuthority ?? fetchMe;
  let transition = options.transition;
  let latestMe: MeResponse | null = null;
  let latestContext: OfflineCacheContext | null = null;

  for (let attempt = 0; attempt < MAX_RECONCILIATION_ATTEMPTS; attempt += 1) {
    const freshness = await prepareOfflineFileCacheContext();

    if (transition && offlineFileCacheTransitionWasLost(transition, freshness)) {
      transition = await beginOfflineAuthTransition();
      continue;
    }

    if (!freshness.generation) {
      latestMe = await withinAuthTransitionTimeout(fetchAuthority());
      latestContext = offlineCacheContextForMe(latestMe);
      return {
        me: latestMe,
        context: latestContext,
        lease: null,
        transition,
        workerAvailable: false,
      };
    }

    if (!transition && offlineFileCacheFreshnessNeedsTransition(freshness)) {
      transition = await beginOfflineAuthTransition();
      continue;
    }

    latestMe = await withinAuthTransitionTimeout(fetchAuthority());
    latestContext = offlineCacheContextForMe(latestMe);

    if (!latestContext) {
      if (transition) {
        const finished = await finishOfflineFileCacheTransition(
          transition,
          freshness,
        );
        if (!finished && attempt + 1 < MAX_RECONCILIATION_ATTEMPTS) {
          transition = await beginOfflineAuthTransition();
          continue;
        }
      }
      return {
        me: latestMe,
        context: null,
        lease: null,
        transition,
        workerAvailable: true,
      };
    }

    if (
      !transition &&
      offlineFileCacheContextNeedsTransition(latestContext, freshness)
    ) {
      transition = await beginOfflineAuthTransition();
      continue;
    }

    const lease = await setOfflineFileCacheContext(
      latestContext,
      freshness,
      transition,
    );
    if (lease) {
      return {
        me: latestMe,
        context: latestContext,
        lease,
        transition,
        workerAvailable: true,
      };
    }

    // A missing/rejected completion keeps the local barrier closed. Start a
    // new capability only after another fresh probe; this covers a Worker that
    // restarted between begin and completion without treating failure as open.
    if (attempt + 1 < MAX_RECONCILIATION_ATTEMPTS) {
      transition = await beginOfflineAuthTransition();
      continue;
    }
  }

  if (!latestMe) {
    latestMe = await withinAuthTransitionTimeout(fetchAuthority());
    latestContext = offlineCacheContextForMe(latestMe);
  }
  return {
    me: latestMe,
    context: latestContext,
    lease: null,
    transition,
    workerAvailable: false,
  };
}
