import { useEffect, useState } from "react";

import { fetchMe, type MeResponse } from "@/api";
import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities } from "@/layout/types";
import { AdminPage } from "@/pages/admin";
import { ArchivePage } from "@/pages/archive";
import { FileBrowser } from "@/pages/archive/FileBrowser";
import { demoStats, demoTranslate } from "@/pages/archive/demoData";
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

function capabilitiesFromMe(me: MeResponse): ShellCapabilities {
  const vault = me.vault;
  return {
    vaultName: vault?.name ?? "FrostVault",
    isVaultOwner: Boolean(vault?.is_vault_owner),
    canOperate: Boolean(vault?.can_operate),
    isAdmin: me.is_admin,
    locale: me.locale,
    locales: me.locales,
    vaults: vault
      ? [
          {
            id: vault.id,
            slug: vault.slug,
            name: vault.name,
            role: vault.role,
          },
        ]
      : [],
    role: vault?.role,
  };
}

const fallbackCapabilities: ShellCapabilities = {
  vaultName: "Test Archive",
  isVaultOwner: true,
  canOperate: true,
  isAdmin: true,
  locale: "en",
  locales: ["en", "it"],
  vaults: [
    { id: 1, slug: "test", name: "Test Archive", role: "owner" },
    { id: 2, slug: "other", name: "Other Vault", role: "viewer" },
  ],
  role: "owner",
};

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
  const [pathname, setPathname] = useState(currentPathname);
  const [me, setMe] = useState<MeResponse | null>(null);

  useEffect(() => {
    const onPop = () => setPathname(currentPathname());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    if (pathname === "/login") return;

    let cancelled = false;
    void fetchMe()
      .then((data) => {
        if (!cancelled) setMe(data);
      })
      .catch(() => {
        if (!cancelled) setMe(null);
      });
    return () => {
      cancelled = true;
    };
  }, [pathname]);

  const navigate = (path: string) => {
    window.history.pushState({}, "", path);
    setPathname(path);
  };

  if (isVaultCreateRecoveryDemo()) {
    return <VaultCreateScreenshotFixture />;
  }

  if (pathname === "/login") {
    return <LoginPage />;
  }

  if (pathname === "/no-vault") {
    return <NoVaultPage />;
  }

  if (pathname === "/vaults/new") {
    return (
      <VaultCreatePage
        displayName={me?.display_name ?? "Local Admin"}
        onNavigate={navigate}
      />
    );
  }

  if (pathIsAdmin(pathname)) {
    return <AdminPage />;
  }

  if (pathIsVaultAccess(pathname)) {
    const vaultId = me?.vault?.id ?? 1;
    const vaultName = me?.vault?.name ?? fallbackCapabilities.vaultName;
    return (
      <VaultAccessPage
        vaultId={vaultId}
        vaultName={vaultName}
        isAdmin={Boolean(me?.is_admin)}
        onBack={() => navigate("/")}
        onTransferred={() => navigate("/")}
      />
    );
  }

  const capabilities = me ? capabilitiesFromMe(me) : fallbackCapabilities;
  const jobDemo = new URLSearchParams(window.location.search).get("job") === "1";
  const statsForPage = jobDemo
    ? {
        ...demoStats,
        active_jobs: 1,
        filesystem: {
          ok: true,
          uid: 1000,
          gid: 1000,
          checks: [],
          findings: [],
        },
      }
    : demoStats;

  return (
    <AppShell
      capabilities={capabilities}
      handlers={{
        onManageAccess: () => navigate("/vault/access"),
        onAdministration: () => navigate("/admin"),
        onNewVault: () => navigate("/vaults/new"),
      }}
    >
      <ArchivePage
        vaultName={capabilities.vaultName}
        displayName={me?.display_name ?? "Local Admin"}
        stats={statsForPage}
        t={demoTranslate}
        fileList={
          <FileBrowser
            t={demoTranslate}
            vaultName={capabilities.vaultName}
            capabilities={{
              role: capabilities.role ?? "owner",
              can_operate: capabilities.canOperate,
              delete_enabled: me?.vault?.delete_enabled ?? true,
              cloud_deletion_enabled:
                me?.vault?.cloud_deletion_enabled ?? true,
              is_vault_owner: capabilities.isVaultOwner,
            }}
          />
        }
      />
    </AppShell>
  );
}
