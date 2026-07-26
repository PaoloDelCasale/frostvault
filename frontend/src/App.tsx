import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities } from "@/layout/types";
import { ArchivePage } from "@/pages/archive";
import { demoStats, demoTranslate } from "@/pages/archive/demoData";
import { LoginPage } from "@/pages/login/LoginPage";
import { NoVaultPage } from "@/pages/no-vault/NoVaultPage";
import { VaultCreatePage } from "@/pages/vault-create";
import { VaultCreateScreenshotFixture } from "@/pages/vault-create/VaultCreateScreenshotFixture";

const demoCapabilities: ShellCapabilities = {
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

const demoFiles = [
  "reports/q1-summary.pdf",
  "photos/family-2024/IMG_001.jpg",
  "docs/contracts/lease.pdf",
];

function currentPathname(): string {
  if (typeof window === "undefined") return "/";
  return window.location.pathname;
}

function ArchiveDemo() {
  return (
    <AppShell capabilities={demoCapabilities}>
      <ArchivePage
        vaultName={demoCapabilities.vaultName}
        displayName="Local Admin"
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

export default function App() {
  const pathname = currentPathname();

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
    return <VaultCreatePage displayName="Local Admin" />;
  }

  return <ArchiveDemo />;
}
