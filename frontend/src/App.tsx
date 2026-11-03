import { useCallback, useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  apiQueryKeys,
  fetchMe,
  fetchVaults,
  logout,
  selectVault,
  useCatalogEvents,
  type MeResponse,
  type VaultListItem,
} from "@/api";
import { DEMO_MODE_ENABLED, getDemoSearchParam } from "@/demoGate";
import { useI18n } from "@/i18n";
import { useTheme } from "@/theme";
import { AppShell } from "@/layout/AppShell";
import { shellLabel } from "@/layout/labels";
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
    locale: me.locale,
    locales: me.locales,
    vaults:
      vaults.length > 0
        ? vaults
        : vault
          ? [{ id: vault.id, slug: vault.slug, name: vault.name }]
          : [],
    currentVaultId: vault?.id,
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
  const { t, locale, setLocale } = useI18n();
  const { setUserId } = useTheme();
  const queryClient = useQueryClient();
  const [pathname, setPathname] = useState(currentPathname);
  const [me, setMe] = useState<MeResponse | null>(null);
  const [vaults, setVaults] = useState<VaultListItem[]>([]);
  const [sessionChanging, setSessionChanging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshDocument = useCallback(() => {
    setSessionChanging(true);
    window.location.reload();
  }, []);

  useCatalogEvents({
    vaultId: me?.vault?.id ?? null,
    queryClient,
    enabled: Boolean(me?.vault?.id) && pathname !== "/login" && !sessionChanging,
    onSessionChanged: refreshDocument,
  });

  const navigate = useCallback((path: string) => {
    window.history.pushState({}, "", path);
    setPathname(path);
  }, []);

  useEffect(() => {
    const onPop = () => setPathname(currentPathname());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    if (pathname === "/login" || sessionChanging) return;
    let cancelled = false;
    void fetchMe()
      .then(async (data) => {
        if (cancelled) return;
        setUserId(data.id);
        setMe(data);
        setError(null);
        if (!data.vault && pathname === "/") navigate("/no-vault");
        const listed = await fetchVaults().catch(() => ({ items: [] }));
        if (!cancelled) setVaults(listed.items ?? []);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [locale, navigate, pathname, sessionChanging, setUserId]);

  async function clearSessionData() {
    setSessionChanging(true);
    await queryClient.cancelQueries();
    queryClient.clear();
  }

  if (isVaultCreateRecoveryDemo()) return <VaultCreateScreenshotFixture />;
  if (pathname === "/login") return <LoginPage />;

  if (!me || sessionChanging) {
    return (
      <div className="grid min-h-svh place-items-center bg-canvas text-sm text-muted">
        {error ? <p role="alert">{error}</p> : shellLabel(t, "ui.loading", "Loading…")}
      </div>
    );
  }

  if (pathname === "/no-vault") return <NoVaultPage />;
  if (pathname === "/vaults/new") {
    return <VaultCreatePage displayName={me.display_name} onNavigate={navigate} />;
  }
  if (pathIsAdmin(pathname)) return <AdminPage />;
  if (pathIsVaultAccess(pathname)) {
    const accessVault = me.vault ?? me.decommission_vault;
    if (!accessVault) return <NoVaultPage />;
    return (
      <VaultAccessPage
        vaultId={accessVault.id}
        vaultName={accessVault.name}
        decommissionState={me.decommission_vault?.decommission_state ?? "active"}
        ownerName={me.display_name || me.username}
        onBack={() => navigate("/")}
        onDecommissioned={() => navigate("/")}
      />
    );
  }
  if (!me.vault) return <NoVaultPage />;
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
        onSignOut: () => {
          void (async () => {
            await clearSessionData();
            setUserId(null);
            setMe(null);
            setVaults([]);
            try {
              await logout();
              window.location.assign("/login");
            } catch (err) {
              setError(err instanceof Error ? err.message : String(err));
              setSessionChanging(false);
            }
          })();
        },
        onLocaleChange: (nextLocale) => {
          void setLocale(nextLocale).then(() =>
            queryClient.invalidateQueries({
              queryKey: apiQueryKeys.notifications,
              refetchType: "active",
            }),
          );
        },
        onVaultChange: (vaultId) => {
          void (async () => {
            await clearSessionData();
            try {
              await selectVault({ vault_id: vaultId });
              window.location.assign("/");
            } catch (err) {
              setError(err instanceof Error ? err.message : String(err));
              setSessionChanging(false);
            }
          })();
        },
      }}
    >
      {error ? <p role="alert">{error}</p> : null}
      <ArchivePage
        vaultName={capabilities.vaultName}
        displayName={me.display_name}
        t={t}
        fileList={
          <FileBrowser
            key={me.vault.id}
            t={t}
            vaultId={me.vault.id}
            vaultName={capabilities.vaultName}
            capabilities={{
              delete_enabled: me.vault.delete_enabled,
              cloud_deletion_enabled: me.vault.cloud_deletion_enabled,
            }}
          />
        }
      />
    </AppShell>
  );
}
