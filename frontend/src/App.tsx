import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  apiQueryKeys,
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
  subscribeToOfflineFileCacheInvalidation,
  verifyOfflineFileCacheLease,
  type OfflineCacheContext,
  type OfflineFileCacheLease,
  type OfflineFileCacheTransition,
} from "@/pwa/offlineFiles";
import {
  AuthTransitionTimeoutError,
  beginOfflineAuthTransition,
  offlineCacheContextForMe,
  reconcileOfflineAuthTransition,
  runOfflineAuthMutation,
  withinAuthTransitionTimeout,
  type OfflineAuthReconciliation,
} from "@/pwa/authTransition";
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

function sameOfflineCacheAuthorization(
  left: OfflineCacheContext | null,
  right: OfflineCacheContext | null,
): boolean {
  if (!left || !right) return left === right;
  return (
    left.userId === right.userId &&
    left.vaultId === right.vaultId &&
    left.authorizationGeneration === right.authorizationGeneration
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
    useRef<OfflineCacheContext | null>(null);
  const refreshSessionRef = useRef<
    (transition?: OfflineFileCacheTransition) => Promise<MeResponse>
  >(async () => {
    throw new Error("Session refresh is not initialized");
  });
  const refreshSessionInFlightRef = useRef<Promise<MeResponse> | null>(null);
  const refreshSessionQueuedRef = useRef(false);
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
    // During a Worker transition, replace only the archive content. AppShell
    // stays mounted so its skip link and #main-content landmark never detach.
    setOfflineCacheTransitioning(true);
    void queryClient.cancelQueries({ queryKey: ["files"] });
    queryClient.removeQueries({ queryKey: ["files"] });
  }, [queryClient]);

  const applyOfflineAuthReconciliation = useCallback(
    async (reconciliation: OfflineAuthReconciliation): Promise<MeResponse> => {
      const nextAuthorization = reconciliation.context;
      if (
        !reconciliation.lease ||
        !sameOfflineCacheAuthorization(
          offlineCacheAuthorizationRef.current,
          nextAuthorization,
        )
      ) {
        // The shared helper has already purged persisted listings. Removing
        // React Query data here prevents a prior Session's in-memory response
        // from surviving while this fresh authority renders network-only.
        clearOfflineFileData();
      }
      offlineCacheAuthorizationRef.current = nextAuthorization;
      setOfflineCacheLease(reconciliation.lease);
      setOfflineCacheTransitioning(false);

      const nextMe = reconciliation.me;
      setUserId(nextMe.id);
      setMe(nextMe);
      try {
        const listed = await withinAuthTransitionTimeout(fetchVaults());
        setVaults(listed.items ?? []);
      } catch {
        setVaults([]);
      }
      return nextMe;
    },
    [clearOfflineFileData, setUserId],
  );

  const refreshSession = useCallback(
    (initialTransition?: OfflineFileCacheTransition): Promise<MeResponse> => {
      // Worker broadcasts can arrive while the initial /api/me reconciliation
      // is still running. Coalesce those notifications so they cannot race
      // each other and repeatedly detach the authenticated archive content.
      const existing = refreshSessionInFlightRef.current;
      if (existing) {
        // An invalidation can arrive after reconciliation has updated the
        // authenticated shell but before its final authority fetch settles.
        // Remember that request so the closed barrier is reconciled again
        // instead of leaving the archive content in its transition placeholder.
        refreshSessionQueuedRef.current = true;
        return existing;
      }

      const operation = (async () => {
        const reconciliation = await reconcileOfflineAuthTransition({
          transition: initialTransition,
        });
        return applyOfflineAuthReconciliation(reconciliation);
      })();
      refreshSessionInFlightRef.current = operation;
      void operation.then(
        () => {
          if (refreshSessionInFlightRef.current !== operation) return;
          refreshSessionInFlightRef.current = null;
          if (!refreshSessionQueuedRef.current) return;
          refreshSessionQueuedRef.current = false;
          void refreshSession().catch(() => undefined);
        },
        () => {
          if (refreshSessionInFlightRef.current === operation) {
            refreshSessionInFlightRef.current = null;
            refreshSessionQueuedRef.current = false;
          }
        },
      );
      return operation;
    },
    [applyOfflineAuthReconciliation],
  );

  useEffect(() => {
    refreshSessionRef.current = refreshSession;
  }, [refreshSession]);

  useEffect(
    () =>
      subscribeToOfflineFileCacheInvalidation((invalidation) => {
        clearOfflineFileData();
        if (
          (invalidation.state !== "reconcile" &&
            invalidation.state !== "unknown") ||
          pathname === "/login"
        ) {
          return;
        }
        // Another page completed a transition, or the Worker controller
        // changed and its prior capability is unknowable. This page has no
        // authority to reuse its context, so it obtains a fresh /api/me before
        // any lease. refreshSession is single-flight and queues a follow-up
        // when this event races the initial reconciliation.
        void refreshSessionRef.current().catch(() => undefined);
      }),
    [clearOfflineFileData, pathname],
  );

  useEffect(() => {
    if (!offlineCacheLease || pathname === "/login") return;
    let cancelled = false;
    const verifyLease = () => {
      void verifyOfflineFileCacheLease(
        offlineCacheLease,
        offlineCacheLease.context,
      ).then((valid) => {
        if (cancelled || valid) return;
        clearOfflineFileData();
        // Render fresh authority network-only while the shared helper rebuilds
        // a replacement capability. A previously painted fully offline view
        // can remain visibly stale until this bounded probe runs, but it can
        // neither serve nor persist data after the probe detects a new boot.
        setOfflineCacheTransitioning(false);
        void refreshSessionRef.current().catch(() => undefined);
      });
    };
    verifyLease();
    const onVisibilityChange = () => {
      if (document.visibilityState !== "hidden") verifyLease();
    };
    window.addEventListener("focus", verifyLease);
    window.addEventListener("online", verifyLease);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", verifyLease);
      window.removeEventListener("online", verifyLease);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [clearOfflineFileData, offlineCacheLease, pathname]);

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
        // The bounded helper records a local close even if no Worker exists.
        void beginOfflineAuthTransition();
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

  if (!authChecked || !me) {
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

  const offlineCacheContext = offlineCacheLease?.context ?? offlineCacheContextForMe(me);
  if (!offlineCacheContext) return <NoVaultPage />;

  const capabilities = capabilitiesFromMe(me, vaults);
  const cacheKey = offlineCacheLease
    ? `offline-${offlineCacheLease.context.userId}-${offlineCacheLease.context.vaultId}-${offlineCacheLease.context.authorizationGeneration}-${offlineCacheLease.generation.bootId}-${offlineCacheLease.generation.counter}-${offlineCacheLease.clientGeneration}`
    : `network-only-${offlineCacheContext.userId}-${offlineCacheContext.vaultId}-${offlineCacheContext.authorizationGeneration}`;

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
            const transition = await beginOfflineAuthTransition();
            setUserId(null);
            setMe(null);
            setVaults([]);
            try {
              await withinAuthTransitionTimeout(logout());
              // Logout deliberately leaves the Worker/local barrier closed.
              // The next sign-in must establish a fresh server generation;
              // there is no empty-context acknowledgement that could reopen an
              // old Session after an unresponsive Worker.
            } catch (error) {
              if (!(error instanceof AuthTransitionTimeoutError)) {
                // A known failed logout may recover only through a new /api/me
                // reconciliation; otherwise this browser remains network-only.
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
            try {
              // The coordinator owns close → selection mutation → fresh
              // /api/me → Worker reconciliation. A missing Worker remains
              // deliberately network-only rather than delaying the mutation.
              const outcome = await runOfflineAuthMutation(() =>
                selectVault({ vault_id: vaultId }),
              );
              if (outcome.reconciliation) {
                await applyOfflineAuthReconciliation(outcome.reconciliation);
              }
              window.location.assign("/");
            } catch (error) {
              if (error instanceof AuthTransitionTimeoutError) {
                // The server request may still commit. Do not reopen based on
                // an earlier /api/me; stay closed/network-only instead.
                return;
              }
              // A definitive mutation failure still requires a fresh /api/me
              // before the old context can be registered again.
              await refreshSession().catch(() => undefined);
            }
          })();
        },
      }}
    >
      {offlineCacheTransitioning ? (
        <div
          role="status"
          aria-live="polite"
          className="grid min-h-[12rem] place-items-center text-sm text-muted"
        >
          {shellLabel(t, "ui.loading", "Loading…")}
        </div>
      ) : (
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
              authorizationGeneration={offlineCacheContext.authorizationGeneration}
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
      )}
      <Toast
        open={Boolean(refreshNotice)}
        message={refreshNotice?.message ?? ""}
        variant={refreshNotice?.error ? "error" : "success"}
        onClose={() => setRefreshNotice(null)}
      />
    </AppShell>
  );
}
