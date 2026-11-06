import { useEffect, useRef } from "react";
import type { QueryClient } from "@tanstack/react-query";

import {
  fetchCatalogRevision,
  openCatalogEventSource,
  type CatalogEventSourceFactory,
  type CatalogRevisionSignal,
  normalizeDomains,
} from "./catalogEvents";
import { apiQueryKeys } from "./query";

/** Leading + trailing throttle, never a debounce: continuous writes cannot starve refresh. */
export const CATALOG_REFRESH_MS = 5_000;
const INITIAL_RECONNECT_MS = 1_000;
const MAX_RECONNECT_MS = 30_000;
const scheduleReconnectTimer = (callback: () => void, ms: number) => window.setTimeout(callback, ms);
const cancelReconnectTimer = (id: number) => window.clearTimeout(id);

export type UseCatalogEventsOptions = {
  vaultId: number | null | undefined;
  queryClient: QueryClient;
  enabled?: boolean;
  explicitVault?: boolean;
  /** Refresh the document when another tab changes this Session's Vault/auth. */
  onSessionChanged?: () => void;
  createSource?: CatalogEventSourceFactory;
  fetchRevision?: typeof fetchCatalogRevision;
  /** Test seam: override timers. */
  schedule?: (callback: () => void, ms: number) => number;
  cancelSchedule?: (id: number) => void;
};

function invalidateForSignal(
  queryClient: QueryClient,
  signal: CatalogRevisionSignal,
  essentialShell = false,
): void {
  const domains = normalizeDomains(signal.domains);
  const tasks: Array<Promise<unknown>> = [];
  if (domains.includes("files") || signal.has_gap) {
    tasks.push(
      queryClient.invalidateQueries({
        queryKey: ["files"],
        refetchType: "active",
        predicate: (query) => typeof query.queryKey[1] !== "number" ||
          query.queryKey[1] === signal.vault_id || Boolean(query.queryKey[2]),
      }, { cancelRefetch: false }),
    );
  }
  if (domains.includes("stats") || signal.has_gap) {
    tasks.push(
      queryClient.invalidateQueries({
        queryKey: apiQueryKeys.stats,
        refetchType: "active",
      }, { cancelRefetch: false }),
    );
  }
  if (essentialShell && (domains.includes("files") || signal.has_gap)) {
    for (const prefix of ["general", "details", "file-detail", "file-job-history", "linked-job", "problems", "notifications"]) {
      tasks.push(queryClient.invalidateQueries({ queryKey: [prefix], refetchType: "active" }, { cancelRefetch: false }));
    }
  }
  void Promise.all(tasks);
}

/**
 * Subscribe to Vault-scoped catalog revisions and invalidate archive queries.
 *
 * - No idle polling: the stream (plus focus/online catch-up) drives refreshes.
 * - Reconnect uses exponential backoff and resumes from the last revision.
 * - Vault switch closes the previous stream before opening the next.
 */
export function useCatalogEvents({
  vaultId,
  queryClient,
  enabled = true,
  explicitVault = false,
  onSessionChanged,
  createSource,
  fetchRevision = fetchCatalogRevision,
  schedule = scheduleReconnectTimer,
  cancelSchedule = cancelReconnectTimer,
}: UseCatalogEventsOptions): void {
  const lastRevisionRef = useRef(0);
  const reconnectAttemptRef = useRef(0);
  const activeVaultRef = useRef<number | null>(null);

  useEffect(() => {
    if (!enabled || vaultId == null || !Number.isSafeInteger(vaultId) || vaultId <= 0) {
      return;
    }

    let closed = false;
    let source: { close: () => void } | null = null;
    let reconnectTimer: number | null = null;
    let refreshTimer: number | null = null;
    let pendingSignal: CatalogRevisionSignal | null = null;
    const clearRefresh = () => {
      if (refreshTimer != null) cancelSchedule(refreshTimer);
      refreshTimer = null;
      pendingSignal = null;
    };
    const flush = () => {
      refreshTimer = null;
      if (closed || !pendingSignal) return;
      const signal = pendingSignal;
      pendingSignal = null;
      invalidateForSignal(queryClient, signal, explicitVault);
      // Keep the cooldown even when a flush emptied the batch.
      refreshTimer = schedule(flush, CATALOG_REFRESH_MS);
    };
    activeVaultRef.current = vaultId;
    // A Vault switch must not inherit another Vault's high-water mark.
    lastRevisionRef.current = 0;
    reconnectAttemptRef.current = 0;

    const clearReconnect = () => {
      if (reconnectTimer != null) {
        cancelSchedule(reconnectTimer);
        reconnectTimer = null;
      }
    };

    const sessionChanged = () => {
      if (closed) return;
      closed = true;
      clearReconnect();
      clearRefresh();
      source?.close();
      source = null;
      onSessionChanged?.();
    };

    const applySignal = (signal: CatalogRevisionSignal) => {
      if (closed || activeVaultRef.current !== vaultId) return;
      if (signal.vault_id !== vaultId) {
        sessionChanged();
        return;
      }
      if (!signal.has_gap && signal.revision <= lastRevisionRef.current) return;
      lastRevisionRef.current = Math.max(lastRevisionRef.current, signal.revision);
      pendingSignal = pendingSignal ? {
        ...signal,
        domains: [...new Set([...pendingSignal.domains, ...normalizeDomains(signal.domains)])],
        has_gap: pendingSignal.has_gap || signal.has_gap,
      } : { ...signal, domains: normalizeDomains(signal.domains) };
      if (refreshTimer == null) flush();
    };

    const connect = () => {
      if (closed || activeVaultRef.current !== vaultId) return;
      clearReconnect();
      source?.close();
      source = openCatalogEventSource(
        lastRevisionRef.current,
        {
          onHello: (hello) => {
            if (closed) return;
            if (hello.vault_id !== vaultId) {
              sessionChanged();
              return;
            }
            // Catch-up catalog frames update the watermark. Hello only proves
            // the stream is authorized for this Vault.
            reconnectAttemptRef.current = 0;
          },
          onCatalog: (signal) => {
            reconnectAttemptRef.current = 0;
            applySignal(signal);
          },
          onError: (error) => {
            if (
              error.error === "vault_switched" ||
              error.error === "vault_access_revoked" ||
              error.error === "session_revoked" ||
              error.error === "session_expired" ||
              error.error === "session_version_mismatch"
            ) {
              sessionChanged();
              return;
            }
            scheduleReconnect();
          },
          onConnectionError: () => {
            source?.close();
            source = null;
            scheduleReconnect();
          },
        },
        createSource,
        explicitVault ? vaultId : undefined,
      );
    };

    const scheduleReconnect = () => {
      if (closed || activeVaultRef.current !== vaultId) return;
      clearReconnect();
      const attempt = reconnectAttemptRef.current;
      reconnectAttemptRef.current = attempt + 1;
      const delay = Math.min(
        MAX_RECONNECT_MS,
        INITIAL_RECONNECT_MS * 2 ** Math.min(attempt, 5),
      );
      reconnectTimer = schedule(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    const catchUp = () => {
      if (closed || activeVaultRef.current !== vaultId) return;
      const snapshot = explicitVault ? fetchRevision(lastRevisionRef.current, undefined, vaultId) : fetchRevision(lastRevisionRef.current);
      void snapshot
        .then((signal) => {
          if (closed || activeVaultRef.current !== vaultId) return;
          if (signal.vault_id !== vaultId) {
            sessionChanged();
            return;
          }
          if (!signal.changed && !signal.has_gap) {
            if (signal.revision > lastRevisionRef.current) {
              lastRevisionRef.current = signal.revision;
            }
            return;
          }
          applySignal(signal);
        })
        .catch(() => {
          // Offline or auth failure: the next focus/online or SSE reconnect
          // will retry. Do not start fixed-interval polling.
        });
    };

    const onFocus = () => catchUp();
    const onOnline = () => {
      catchUp();
      if (!source) connect();
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") catchUp();
    };

    connect();
    window.addEventListener("focus", onFocus);
    window.addEventListener("online", onOnline);
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      closed = true;
      activeVaultRef.current = null;
      clearReconnect();
      clearRefresh();
      source?.close();
      source = null;
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("online", onOnline);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [
    cancelSchedule,
    createSource,
    enabled,
    explicitVault,
    fetchRevision,
    onSessionChanged,
    queryClient,
    schedule,
    vaultId,
  ]);
}
