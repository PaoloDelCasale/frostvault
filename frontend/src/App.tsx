import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  apiQueryKeys,
  fetchMe,
  fetchVaults,
  logout,
  ReauthenticationRedirectError,
  requestScan,
  selectVault,
  type MeResponse,
  type VaultListItem,
} from "@/api";
import { DEMO_MODE_ENABLED, getDemoSearchParam } from "@/demoGate";
import { useI18n } from "@/i18n";
import { useTheme } from "@/theme";
import {
  beginOfflineFileCacheTransition,
  finishOfflineFileCacheTransition,
  invalidateLegacyCachedFilesListings,
  isOfflineCacheContext,
  prepareOfflineFileCacheContext,
  setOfflineFileCacheContext,
  subscribeToOfflineFileCacheInvalidation,
  type OfflineCacheContext,
  type OfflineFileCacheFreshness,
  type OfflineFileCacheLease,
  type OfflineFileCacheTransition,
} from "@/pwa/offlineFiles";
import { AppShell } from "@/layout/AppShell";
import { shellLabel } from "@/layout/labels";
import { Toast } from "@/components/Toast";
import type { ShellCapabilities } from "@/layout/types";
import { AdminPage } from "@/pages/admin";
import { ArchivePage } from "@/pages/archive";
import { FileBrowser } from "@/pages/archive/FileBrowser";
import { LoginPage } from "@/pages/login/LoginPage";
import { NoVaultPage } from "@/pages/no-vault/NoVaultPage";
import { VaultAccessPage } from "@/pages/vault-access";
import { VaultCreatePage } from "@/pages/vault-create";
import { VaultCreateScreenshotFixture } from "@/pages/vault-create/VaultCreateScreenshotFixture";

const SESSION_REQUEST_TIMEOUT_MS = 5_000;

class SessionRequestTimeoutError extends Error {
  constructor() {
    super("Session request timed out");
  }
}

function withinSessionTimeout<T>(work: Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      callback();
    };
    const timeout = setTimeout(
      () => finish(() => reject(new SessionRequestTimeoutError())),
      SESSION_REQUEST_TIMEOUT_MS,
    );
    void work.then(
      (value) => finish(() => resolve(value)),
      (error: unknown) => finish(() => reject(error)),
    );
  });
}

function pathIsVaultAccess(pathname: string): boolean {
  return pathname === "/vault/access" || pathname.startsWith("/vault/access/");
}

function pathIsAdmin(pathname: string): boolean {
  return pathname === "/admin" || pathname.startsWith("/admin/");
}

function capabilitiesFromMe(
  me: MeResponse,
  vaults: VaultListItem[],
): ShellCapabilities {
  const vault = me.vault;
  return {
    vaultName: vault?.name ?? "FrostVault",
    isVaultOwner: Boolean(vault?.is_vault_owner),
    canOperate: Boolean(vault?.can_operate),
    isAdmin: me.is_admin,
    locale: me.locale,
    locales: me.locales,
    vaults:
      vaults.length > 0
        ? vaults
        : vault
          ? [
              {
                id: vault.id,
                slug: vault.slug,
                name: vault.name,
                role: vault.role,
              },
            ]
          : [],
    currentVaultId: vault?.id,
    role: vault?.role,
  };
}

function isVaultCreateRecoveryDemo(): boolean {
  return (
    DEMO_MODE_ENABLED &&
    getDemoSearchParam("demo") === "vault-create-recovery"
  );
}

function currentPathname(): string {
  if (typeof window === "undefined") return "/";
  return window.location.pathname;
}

function offlineCacheContextFor(me: MeResponse): OfflineCacheContext | null {
  if (!me.vault) return null;
  const context = { userId: me.id, vaultId: me.vault.id };
  return isOfflineCacheContext(context) ? context : null;
}

type OfflineCacheAuthorization = Readonly<{
  context: OfflineCacheContext;
  // csrf_token is per Session and remains in memory only. It distinguishes a
  // real Session replacement from ordinary refreshes of the same User/Vault.
  sessionFingerprint: string;
}>;

type OfflineCacheSynchronization =
  | "ready"
  | "retry"
  | "network-only"
  | "start-transition";

function offlineCacheAuthorizationFor(
  me: MeResponse,
): OfflineCacheAuthorization | null {
  const context = offlineCacheContextFor(me);
  if (!context) return null;
  return {
    context,
    sessionFingerprint: [
      me.session_version,
      me.auth_method,
      me.csrf_token,
    ].join("\u0000"),
  };
}

function sameOfflineCacheAuthorization(
  left: OfflineCacheAuthorization | null,
  right: OfflineCacheAuthorization | null,
): boolean {
  if (!left || !right) return left === right;
  return (
    left.context.userId === right.context.userId &&
    left.context.vaultId === right.context.vaultId &&
    left.sessionFingerprint === right.sessionFingerprint
  );
}

export default function App() {
  const { t, setLocale } = useI18n();
  const { setUserId } = useTheme();
  const queryClient = useQueryClient();
  const [pathname, setPathname] = useState(currentPathname);
  const [me, setMe] = useState<MeResponse | null>(null);
  const [vaults, setVaults] = useState<VaultListItem[]>([]);
  const [authChecked, setAuthChecked] = useState(pathname === "/login");
  const [offlineCacheLease, setOfflineCacheLease] =
    useState<OfflineFileCacheLease | null>(null);
  const [offlineCacheTransitioning, setOfflineCacheTransitioning] =
    useState(false);
  const offlineCacheAuthorizationRef =
    useRef<OfflineCacheAuthorization | null>(null);
  const refreshSessionRef = useRef<
    (transition?: OfflineFileCacheTransition) => Promise<MeResponse>
  >(async () => {
    throw new Error("Session refresh is not initialized");
  });
  const [refreshNotice, setRefreshNotice] = useState<{
    message: string;
    error: boolean;
  } | null>(null);

  const navigate = useCallback((path: string) => {
    window.history.pushState({}, "", path);
    setPathname(path);
  }, []);

  const clearOfflineFileData = useCallback(() => {
    offlineCacheAuthorizationRef.current = null;
    setOfflineCacheLease(null);
    // During a Worker transition, do not leave a FileBrowser that can refetch
    // using the old server Session mounted behind a newly cleared lease.
    setOfflineCacheTransitioning(true);
    void queryClient.cancelQueries({ queryKey: ["files"] });
    queryClient.removeQueries({ queryKey: ["files"] });
  }, [queryClient]);

  const synchronizeOfflineCacheContext = useCallback(
    async (
      nextMe: MeResponse,
      freshness: OfflineFileCacheFreshness,
      transition?: OfflineFileCacheTransition,
    ): Promise<OfflineCacheSynchronization> => {
      invalidateLegacyCachedFilesListings();
      const nextAuthorization = offlineCacheAuthorizationFor(nextMe);
      const currentAuthorization = offlineCacheAuthorizationRef.current;

      if (
        !sameOfflineCacheAuthorization(
          currentAuthorization,
          nextAuthorization,
        ) &&
        currentAuthorization &&
        !transition
      ) {
        // This /api/me response proves that the Session/Vault changed, but was
        // collected before the global close. Start a new transition and fetch
        // again rather than using it to revive a cache context.
        clearOfflineFileData();
        return "start-transition";
      }

      if (!nextAuthorization) {
        clearOfflineFileData();
        if (transition) {
          await finishOfflineFileCacheTransition(transition, freshness);
        }
        // No Vault has no file-list cache surface; the normal no-Vault page is
        // safe even if Worker coordination is unavailable.
        setOfflineCacheTransitioning(false);
        return "network-only";
      }

      if (!freshness.generation) {
        // The page has a fresh authoritative Session but no reachable Worker.
        // Render network-only; FileBrowser sends no lease header and cannot
        // read/write the local listing cache.
        clearOfflineFileData();
        setOfflineCacheTransitioning(false);
        return "network-only";
      }

      const lease = await setOfflineFileCacheContext(
        nextAuthorization.context,
        freshness,
        transition,
      );
      if (!lease) {
        // A missing ACK, a closed transition owned by another tab, or a Worker
        // restart is fail-closed. The fresh /api/me response remains safe to
        // render network-only, but never grants offline persistence.
        clearOfflineFileData();
        setOfflineCacheTransitioning(false);
        return "network-only";
      }

      offlineCacheAuthorizationRef.current = nextAuthorization;
      setOfflineCacheLease(lease);
      setOfflineCacheTransitioning(false);
      return "ready";
    },
    [clearOfflineFileData],
  );

  const refreshSession = useCallback(
    async (initialTransition?: OfflineFileCacheTransition) => {
      let transition = initialTransition;
      let nextMe: MeResponse | null = null;
      let synchronized = false;

      // A context registration only follows a fresh /api/me. If the response
      // discovers an unannounced Session replacement, begin closes globally
      // before this loop obtains the replacement response again.
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const freshness = await prepareOfflineFileCacheContext();
        const candidate = await withinSessionTimeout(fetchMe());
        nextMe = candidate;
        const result = await synchronizeOfflineCacheContext(
          candidate,
          freshness,
          transition,
        );
        if (result === "start-transition") {
          transition = await beginOfflineFileCacheTransition();
          continue;
        }
        if (result === "retry") continue;
        synchronized = true;
        break;
      }

      if (!nextMe) throw new Error("/api/me did not return a response");
      if (!synchronized) {
        // Bounded retries exhausted: local data is gone and the app can still
        // use its authoritative response through the Worker NetworkOnly path.
        clearOfflineFileData();
        setOfflineCacheTransitioning(false);
      }

      setUserId(nextMe.id);
      setMe(nextMe);
      try {
        const listed = await withinSessionTimeout(fetchVaults());
        setVaults(listed.items ?? []);
      } catch {
        setVaults([]);
      }
      return nextMe;
    },
    [clearOfflineFileData, setUserId, synchronizeOfflineCacheContext],
  );

  useEffect(() => {
    refreshSessionRef.current = refreshSession;
  }, [refreshSession]);

  useEffect(
    () =>
      subscribeToOfflineFileCacheInvalidation((invalidation) => {
        clearOfflineFileData();
        if (invalidation.state !== "open" || pathname === "/login") return;
        // The closing tab completes the context itself. Every other tab waits
        // for this reopened-generation broadcast, then gets its own fresh
        // /api/me before requesting a new lease.
        void refreshSessionRef.current().catch(() => undefined);
      }),
    [clearOfflineFileData, pathname],
  );

  const onRefreshList = useCallback(() => {
    void requestScan()
      .then(async (result) => {
        setRefreshNotice({ message: result.message, error: false });
        await Promise.all([
          queryClient.invalidateQueries({
            queryKey: ["files"],
            refetchType: "active",
          }),
          queryClient.invalidateQueries({
            queryKey: apiQueryKeys.stats,
            refetchType: "active",
          }),
          queryClient.invalidateQueries({
            queryKey: ["rename-candidates"],
            refetchType: "active",
          }),
        ]);
      })
      .catch((error: unknown) => {
        // OIDC reauthentication has already navigated away; do not replace it
        // with a stale toast. Every other failure remains visible to the user.
        if (error instanceof ReauthenticationRedirectError) return;
        const message =
          error instanceof ApiError && error.messageKey
            ? error.message
            : t("ui.refresh_list_failed");
        setRefreshNotice({ message, error: true });
      });
  }, [queryClient, t]);

  useEffect(() => {
    const onPop = () => setPathname(currentPathname());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    if (pathname === "/login") {
      setAuthChecked(true);
      return;
    }

    let cancelled = false;
    setAuthChecked(false);
    void refreshSession()
      .then((data) => {
        if (cancelled) return;
        if (!data.vault && pathname === "/") {
          navigate("/no-vault");
        }
      })
      .catch(() => {
        if (cancelled) return;
        // No request/ACK wait is allowed to leave stale persisted data alive.
        // Starting this closes globally but does not delay navigation.
        void beginOfflineFileCacheTransition();
        clearOfflineFileData();
        setUserId(null);
        setMe(null);
        setVaults([]);
        if (pathname !== "/login") {
          window.location.assign("/login");
        }
      })
      .finally(() => {
        if (!cancelled) setAuthChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, [clearOfflineFileData, navigate, pathname, refreshSession, setUserId]);

  if (isVaultCreateRecoveryDemo()) {
    return <VaultCreateScreenshotFixture />;
  }

  if (pathname === "/login") {
    return <LoginPage />;
  }

  if (!authChecked || !me || offlineCacheTransitioning) {
    return (
      <div className="grid min-h-svh place-items-center bg-canvas text-sm text-muted">
        {shellLabel(t, "ui.loading", "Loading…")}
      </div>
    );
  }

  if (pathname === "/no-vault") {
    return <NoVaultPage />;
  }

  if (pathname === "/vaults/new") {
    return (
      <VaultCreatePage
        displayName={me.display_name}
        onNavigate={navigate}
      />
    );
  }

  if (pathIsAdmin(pathname)) {
    return <AdminPage />;
  }

  if (pathIsVaultAccess(pathname)) {
    const accessVault = me.vault ?? me.decommission_vault;
    if (!accessVault) return <NoVaultPage />;
    return (
      <VaultAccessPage
        vaultId={accessVault.id}
        vaultName={accessVault.name}
        decommissionState={
          me.decommission_vault?.decommission_state ?? "active"
        }
        isAdmin={Boolean(me.is_admin)}
        isVaultOwner={Boolean(me.vault?.is_vault_owner)}
        onBack={() => navigate("/")}
        onTransferred={() => navigate("/")}
      />
    );
  }

  const offlineCacheContext = offlineCacheLease?.context ?? offlineCacheContextFor(me);
  if (!offlineCacheContext) return <NoVaultPage />;

  const capabilities = capabilitiesFromMe(me, vaults);
  const cacheKey = offlineCacheLease
    ? `offline-${offlineCacheLease.context.userId}-${offlineCacheLease.context.vaultId}-${offlineCacheLease.generation.bootId}-${offlineCacheLease.generation.counter}-${offlineCacheLease.clientGeneration}`
    : `network-only-${offlineCacheContext.userId}-${offlineCacheContext.vaultId}`;

  return (
    <AppShell
      capabilities={capabilities}
      t={t}
      queryClient={queryClient}
      handlers={{
        onManageAccess: () => navigate("/vault/access"),
        onAdministration: () => navigate("/admin"),
        onNewVault: () => navigate("/vaults/new"),
        onRefreshList,
        onSignOut: () => {
          void (async () => {
            clearOfflineFileData();
            const transition = await beginOfflineFileCacheTransition();
            setUserId(null);
            setMe(null);
            setVaults([]);
            try {
              await withinSessionTimeout(logout());
              // A successful logout is followed by a bounded authoritative
              // probe. A 401 proves there is no context to reopen; it can
              // safely rotate the Worker to an empty, network-only generation.
              const freshness = await prepareOfflineFileCacheContext();
              try {
                const current = await withinSessionTimeout(fetchMe());
                const context = offlineCacheContextFor(current);
                if (context) {
                  await setOfflineFileCacheContext(context, freshness, transition);
                } else {
                  await finishOfflineFileCacheTransition(transition, freshness);
                }
              } catch (error) {
                if (error instanceof ApiError && error.status === 401) {
                  await finishOfflineFileCacheTransition(transition, freshness);
                }
              }
            } catch (error) {
              if (!(error instanceof SessionRequestTimeoutError)) {
                // A known failed logout must fetch fresh authority before it
                // can reopen the prior Session; otherwise remain network-only.
                await refreshSession(transition).catch(() => undefined);
              }
            } finally {
              window.location.assign("/login");
            }
          })();
        },
        onLocaleChange: (locale) => {
          void setLocale(locale).then(async () => {
            await queryClient.invalidateQueries({
              queryKey: apiQueryKeys.notifications,
              refetchType: "active",
            });
            await refreshSession();
          });
        },
        onVaultChange: (vaultId) => {
          void (async () => {
            clearOfflineFileData();
            const transition = await beginOfflineFileCacheTransition();
            try {
              // The mutation starts after a bounded close acknowledgement. If
              // a Worker is gone, begin returns network-only rather than
              // blocking the server-side Vault selection.
              await withinSessionTimeout(selectVault({ vault_id: vaultId }));
              await refreshSession(transition);
              window.location.assign("/");
            } catch (error) {
              if (error instanceof SessionRequestTimeoutError) {
                // The server request may still commit. Do not reopen based on
                // an earlier /api/me; stay closed/network-only instead.
                return;
              }
              // A definitive mutation failure still requires a fresh /api/me
              // before the old context can be registered again.
              await refreshSession(transition).catch(() => undefined);
            }
          })();
        },
      }}
    >
      <ArchivePage
        vaultName={capabilities.vaultName}
        displayName={me.display_name}
        t={t}
        fileList={
          <FileBrowser
            key={cacheKey}
            t={t}
            userId={offlineCacheContext.userId}
            vaultId={offlineCacheContext.vaultId}
            offlineCacheLease={offlineCacheLease}
            vaultName={capabilities.vaultName}
            capabilities={{
              role: capabilities.role ?? "viewer",
              can_operate: capabilities.canOperate,
              delete_enabled: me.vault?.delete_enabled ?? false,
              cloud_deletion_enabled:
                me.vault?.cloud_deletion_enabled ?? false,
              is_vault_owner: capabilities.isVaultOwner,
            }}
          />
        }
      />
      <Toast
        open={Boolean(refreshNotice)}
        message={refreshNotice?.message ?? ""}
        variant={refreshNotice?.error ? "error" : "success"}
        onClose={() => setRefreshNotice(null)}
      />
    </AppShell>
  );
}
