import { useState } from "react";

import { Badge } from "@/components/Badge";
import { Card } from "@/components/Card";
import { Panel } from "@/components/Panel";
import { ProgressBar } from "@/components/ProgressBar";
import { StorageBadge } from "@/components/StorageBadge";
import { Toast } from "@/components/Toast";
import { Button } from "@/components/ui/button";
import { AppShell } from "@/layout/AppShell";
import type { ShellCapabilities } from "@/layout/types";
import { LoginPage } from "@/pages/login/LoginPage";
import { NoVaultPage } from "@/pages/no-vault/NoVaultPage";

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

function currentPathname(): string {
  if (typeof window === "undefined") return "/";
  return window.location.pathname;
}

function DesignSystemDemo() {
  const [toastOpen, setToastOpen] = useState(false);

  return (
    <AppShell capabilities={demoCapabilities}>
      <div className="grid gap-4">
        <Card>
          <span className="text-[13px] text-muted">Design system shell</span>
          <strong className="mt-1 block text-[27px]">Responsive base</strong>
          <p className="mt-2 text-sm text-muted">
            Drawer navigation below md; horizontal controls from md up. Tap targets
            are at least 44×44px.
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button type="button" variant="primary">
              Primary
            </Button>
            <Button type="button" variant="secondary">
              Secondary
            </Button>
            <Button type="button" variant="danger">
              Danger
            </Button>
            <Button type="button" variant="secondary" onClick={() => setToastOpen(true)}>
              Show toast
            </Button>
          </div>
        </Card>

        <Panel>
          <div className="flex flex-wrap gap-2 border-b border-line p-4">
            <Badge state="both" />
            <Badge state="local_only" />
            <Badge state="cloud_only" />
            <StorageBadge storage="standard" />
            <StorageBadge storage="glacier" />
            <StorageBadge storage="deep-archive" />
          </div>
          <div className="p-4">
            <ProgressBar value={62} label="Upload" detail="1.2 GB of 2.0 GB" />
          </div>
        </Panel>
      </div>

      <Toast
        open={toastOpen}
        message="Design system ready"
        variant="success"
        onClose={() => setToastOpen(false)}
      />
    </AppShell>
  );
}

export default function App() {
  const pathname = currentPathname();

  if (pathname === "/login") {
    return <LoginPage />;
  }

  // Temporary route until the archive shell (#65) owns /api/me-based entry.
  if (pathname === "/no-vault") {
    return <NoVaultPage />;
  }

  return <DesignSystemDemo />;
}
