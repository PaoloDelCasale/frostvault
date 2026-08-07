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

const INITIAL_RECONNECT_MS = 1_000;
const MAX_RECONNECT_MS = 30_000;

export type UseCatalogEventsOptions = {
  vaultId: number | null | undefined;
  queryClient: QueryClient;
  enabled?: boolean;
  createSource?: CatalogEventSourceFactory;
  fetchRevision?: typeof fetchCatalogRevision;
  /** Test seam: override timers. */
  schedule?: (callback: () => void, ms: number) => number;
  cancelSchedule?: (id: number) => void;
};

function invalidateForSignal(
  queryClient: QueryClient,
  signal: CatalogRevisionSignal,
): void {
  const domains = normalizeDomains(signal.domains);
  const tasks: Array<Promise<unknown>> = [];
  if (domains.includes("files") || signal.has_gap) {
    tasks.push(
      queryClient.invalidateQueries({
        queryKey: ["files"],
        refetchType: "active",
      }),
    );
  }
  if (domains.includes("stats") || signal.has_gap) {
    tasks.push(
      queryClient.invalidateQueries({
        queryKey: apiQueryKeys.stats,
        refetchType: "active",
      }),
    );
  }
  if (domains.includes("rename_candidates") || signal.has_gap) {
    tasks.push(
      queryClient.invalidateQueries({
        queryKey: ["rename-candidates"],
        refetchType: "active",
      }),
    );
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
  createSource,
  fetchRevision = fetchCatalogRevision,
  schedule = (callback, ms) => window.setTimeout(callback, ms),
  cancelSchedule = (id) => window.clearTimeout(id),
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

    const applySignal = (signal: CatalogRevisionSignal) => {
      if (closed || activeVaultRef.current !== vaultId) return;
      if (signal.vault_id !== vaultId) return;
      if (!signal.has_gap && signal.revision <= lastRevisionRef.current) return;
      lastRevisionRef.current = Math.max(lastRevisionRef.current, signal.revision);
      invalidateForSignal(queryClient, signal);
    };

    const connect = () => {
      if (closed || activeVaultRef.current !== vaultId) return;
      clearReconnect();
      source?.close();
      source = openCatalogEventSource(
        lastRevisionRef.current,
        {
          onHello: (hello) => {
            if (closed || hello.vault_id !== vaultId) return;
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
              error.error === "vault_access_revoked"
            ) {
              source?.close();
              source = null;
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
      void fetchRevision(lastRevisionRef.current)
        .then((signal) => {
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
    fetchRevision,
    queryClient,
    schedule,
    vaultId,
  ]);
}
