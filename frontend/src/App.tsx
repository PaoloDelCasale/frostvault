import { useCallback, useEffect, useState } from "react";

import {
  fetchMe,
  fetchVaults,
  logout,
  selectVault,
  type MeResponse,
  type VaultListItem,
} from "@/api";
import { useI18n } from "@/i18n";
import { useTheme } from "@/theme";
import { AppShell } from "@/layout/AppShell";
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
  if (typeof window === "undefined") return false;
  return (
    new URLSearchParams(window.location.search).get("demo") ===
    "vault-create-recovery"
  );
}

function currentPathname(): string {
  if (typeof window === "undefined") return "/";
  return window.location.pathname;
}

export default function App() {
  const { t, setLocale } = useI18n();
  const { setUserId } = useTheme();
  const [pathname, setPathname] = useState(currentPathname);
  const [me, setMe] = useState<MeResponse | null>(null);
  const [vaults, setVaults] = useState<VaultListItem[]>([]);
  const [authChecked, setAuthChecked] = useState(pathname === "/login");

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
        Loading…
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
    const vaultId = me.vault?.id ?? 1;
    const vaultName = me.vault?.name ?? "FrostVault";
    return (
      <VaultAccessPage
        vaultId={vaultId}
        vaultName={vaultName}
        isAdmin={Boolean(me.is_admin)}
        onBack={() => navigate("/")}
        onTransferred={() => navigate("/")}
      />
    );
  }

  const capabilities = capabilitiesFromMe(me, vaults);

  return (
    <AppShell
      capabilities={capabilities}
      handlers={{
        onManageAccess: () => navigate("/vault/access"),
        onAdministration: () => navigate("/admin"),
        onNewVault: () => navigate("/vaults/new"),
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
    </AppShell>
  );
}
