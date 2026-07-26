import { useEffect, useState } from "react";

import { fetchMe, type MeResponse } from "@/api";
import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities } from "@/layout/types";
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
    role: vault?.role ?? null,
  };
}

export default function App() {
  const [pathname, setPathname] = useState(() =>
    typeof window !== "undefined" ? window.location.pathname : "/",
  );
  const [me, setMe] = useState<MeResponse | null>(null);

  useEffect(() => {
    const onPop = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
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
  }, []);

  const navigate = (path: string) => {
    window.history.pushState({}, "", path);
    setPathname(path);
  };

  if (pathIsVaultAccess(pathname)) {
    const vaultId = me?.vault?.id ?? 1;
    const vaultName = me?.vault?.name ?? "FrostVault";
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

  const demoCapabilities: ShellCapabilities = me
    ? capabilitiesFromMe(me)
    : {
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

  return (
    <AppShell
      capabilities={demoCapabilities}
      handlers={{
        onManageAccess: () => navigate("/vault/access"),
      }}
    >
      <div className="grid gap-4 px-1">
        <p className="text-sm text-muted">
          Design system shell — open Manage access for vault governance.
        </p>
      </div>
    </AppShell>
  );
}
