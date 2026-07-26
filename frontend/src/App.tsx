import { useEffect, useState } from "react";

import { fetchMe, type MeResponse } from "@/api";
import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities } from "@/layout/types";
import { ArchivePage } from "@/pages/archive";
import { demoStats, demoTranslate } from "@/pages/archive/demoData";
import { LoginPage } from "@/pages/login/LoginPage";
import { NoVaultPage } from "@/pages/no-vault/NoVaultPage";
import { VaultAccessPage } from "@/pages/vault-access";

function pathIsVaultAccess(pathname: string): boolean {
  return pathname === "/vault/access" || pathname.startsWith("/vault/access/");
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

const demoFiles = [
  "reports/q1-summary.pdf",
  "photos/family-2024/IMG_001.jpg",
  "docs/contracts/lease.pdf",
];

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

  if (pathname === "/login") {
    return <LoginPage />;
  }

  if (pathname === "/no-vault") {
    return <NoVaultPage />;
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

  return (
    <AppShell
      capabilities={capabilities}
      handlers={{
        onManageAccess: () => navigate("/vault/access"),
      }}
    >
      <ArchivePage
        vaultName={capabilities.vaultName}
        displayName={me?.display_name ?? "Local Admin"}
        stats={demoStats}
        t={demoTranslate}
        fileList={
          <ul className="divide-y divide-line">
            {demoFiles.map((path) => (
              <li
                key={path}
                className="flex min-h-11 items-center py-2 text-sm first:pt-0"
              >
                <span className="truncate font-medium">{path}</span>
              </li>
            ))}
          </ul>
        }
      />
    </AppShell>
  );
}
