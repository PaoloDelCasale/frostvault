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
  clearOfflineFileCache,
  invalidateLegacyCachedFilesListings,
  isOfflineCacheContext,
  prepareOfflineFileCacheContext,
  runWithOfflineFileCacheBarrier,
  setOfflineFileCacheContext,
  subscribeToOfflineFileCacheInvalidation,
  type OfflineCacheContext,
  type OfflineFileCacheFreshness,
  type OfflineFileCacheLease,
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

type OfflineCacheSynchronization = "ready" | "retry" | "none";

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
  const offlineCacheAuthorizationRef =
    useRef<OfflineCacheAuthorization | null>(null);
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
    void queryClient.cancelQueries({ queryKey: ["files"] });
    queryClient.removeQueries({ queryKey: ["files"] });
  }, [queryClient]);

  useEffect(
    () => subscribeToOfflineFileCacheInvalidation(clearOfflineFileData),
    [clearOfflineFileData],
  );

  const synchronizeOfflineCacheContext = useCallback(
    async (
      nextMe: MeResponse,
      freshness: OfflineFileCacheFreshness,
    ): Promise<OfflineCacheSynchronization> => {
      invalidateLegacyCachedFilesListings();
      const nextAuthorization = offlineCacheAuthorizationFor(nextMe);
      const currentAuthorization = offlineCacheAuthorizationRef.current;

      if (
        !sameOfflineCacheAuthorization(
          currentAuthorization,
          nextAuthorization,
        )
      ) {
        if (currentAuthorization) {
          // A User, Vault, or per-Session CSRF token changed. The response that
          // revealed that transition predates the clear, so fetch /api/me again
          // under the new Worker epoch before granting a cache lease.
          await clearOfflineFileCache();
          return nextAuthorization ? "retry" : "none";
        }
        if (!nextAuthorization) {
          clearOfflineFileData();
          return "none";
        }
      }

      if (!nextAuthorization) {
        clearOfflineFileData();
        return "none";
      }

      const lease = await setOfflineFileCacheContext(
        nextAuthorization.context,
        freshness,
      );
      if (!lease) {
        // An invalidation raced with this /api/me. Do not render its response
        // or persist its listing; the caller will obtain a new fresh response.
        clearOfflineFileData();
        return "retry";
      }

      offlineCacheAuthorizationRef.current = nextAuthorization;
      setOfflineCacheLease(lease);
      return "ready";
    },
    [clearOfflineFileData],
  );

  const refreshSession = useCallback(async () => {
    let nextMe: MeResponse | null = null;
    let synchronized = false;

    // A clear that races /api/me invalidates its freshness record. Retry with
    // a response obtained after the current epoch instead of reviving a cache.
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const freshness = await prepareOfflineFileCacheContext();
      const candidate = await fetchMe();
      nextMe = candidate;
      const result = await synchronizeOfflineCacheContext(candidate, freshness);
      if (result === "retry") continue;
      synchronized = true;
      break;
    }

    if (!nextMe) throw new Error("/api/me did not return a response");
    if (!synchronized) clearOfflineFileData();

    setUserId(nextMe.id);
    setMe(nextMe);
    try {
      const listed = await fetchVaults();
      setVaults(listed.items ?? []);
    } catch {
      setVaults([]);
    }
    return nextMe;
  }, [
    clearOfflineFileData,
    setUserId,
    synchronizeOfflineCacheContext,
  ]);

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
      void clearOfflineFileCache();
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
        void clearOfflineFileCache();
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
  }, [navigate, pathname, refreshSession, setUserId]);

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

  // Do not leave the old FileBrowser mounted while a new authorization scope
  // is being resolved (for example, during a Vault switch).
  if (!offlineCacheLease) {
    return (
      <div className="grid min-h-svh place-items-center bg-canvas text-sm text-muted">
        {shellLabel(t, "ui.loading", "Loading…")}
      </div>
    );
  }

  const capabilities = capabilitiesFromMe(me, vaults);

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
          // Clearing local UI/cache state is synchronous; the server logout is
          // held behind the Worker acknowledgement so an old write cannot win.
          const clearBarrier = clearOfflineFileCache();
          setUserId(null);
          setMe(null);
          setVaults([]);
          void (async () => {
            try {
              await clearBarrier;
            } catch {
              // Local invalidation has already happened; never keep the old UI.
            }
            try {
              await logout();
            } catch {
              // Navigation still completes after a best-effort server logout.
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
          void runWithOfflineFileCacheBarrier(() =>
            selectVault({ vault_id: vaultId }),
          ).then(
            () => {
              // The server now selected the new Vault. A failed refresh must
              // leave the barrier in place rather than reviving the old one.
              void refreshSession()
                .then(() => {
                  window.location.assign("/");
                })
                .catch(() => undefined);
            },
            () => {
              // Selection itself failed, so only a fresh /api/me may restore
              // the still-current Vault context.
              void refreshSession().catch(() => undefined);
            },
          );
        },
      }}
    >
      <ArchivePage
        vaultName={capabilities.vaultName}
        displayName={me.display_name}
        t={t}
        fileList={
          <FileBrowser
            key={`offline-${offlineCacheLease.context.userId}-${offlineCacheLease.context.vaultId}-${offlineCacheLease.generation}`}
            t={t}
            userId={offlineCacheLease.context.userId}
            vaultId={offlineCacheLease.context.vaultId}
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
