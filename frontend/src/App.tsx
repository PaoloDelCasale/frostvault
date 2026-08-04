import { useCallback, useEffect, useState } from "react";
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

export default function App() {
  const { t, setLocale } = useI18n();
  const { setUserId } = useTheme();
  const queryClient = useQueryClient();
  const [pathname, setPathname] = useState(currentPathname);
  const [me, setMe] = useState<MeResponse | null>(null);
  const [vaults, setVaults] = useState<VaultListItem[]>([]);
  const [authChecked, setAuthChecked] = useState(pathname === "/login");
  const [refreshNotice, setRefreshNotice] = useState<{
    message: string;
    error: boolean;
  } | null>(null);

  const navigate = useCallback((path: string) => {
    window.history.pushState({}, "", path);
    setPathname(path);
  }, []);

  const refreshSession = useCallback(async () => {
    const nextMe = await fetchMe();
    setUserId(nextMe.id);
    setMe(nextMe);
    try {
      const listed = await fetchVaults();
      setVaults(listed.items ?? []);
    } catch {
      setVaults([]);
    }
    return nextMe;
  }, [setUserId]);

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
          // Clear the identity before reloading so /login cannot first-paint
          // the departing user's palette.
          setUserId(null);
          void logout()
            .catch(() => undefined)
            .finally(() => {
              window.location.assign("/login");
            });
        },
        onLocaleChange: (locale) => {
          void setLocale(locale).then(async () => {
            await queryClient.invalidateQueries({
              queryKey: apiQueryKeys.notifications,
              refetchType: "active",
            });
            const nextMe = await fetchMe();
            setMe(nextMe);
          });
        },
        onVaultChange: (vaultId) => {
          void selectVault({ vault_id: vaultId })
            .then(() => refreshSession())
            .then(() => {
              window.location.assign("/");
            });
        },
      }}
    >
      <ArchivePage
        vaultName={capabilities.vaultName}
        displayName={me.display_name}
        t={t}
        fileList={
          <FileBrowser
            t={t}
            vaultId={me.vault?.id ?? capabilities.currentVaultId ?? 0}
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
