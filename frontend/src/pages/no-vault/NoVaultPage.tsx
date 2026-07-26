import { useState } from "react";

import { logout } from "@/api/endpoints";
import { AuthCard } from "@/components/AuthCard";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

type NoVaultPageProps = {
  onNavigate?: (url: string) => void;
};

function defaultNavigate(url: string): void {
  window.location.assign(url);
}

export function NoVaultPage({ onNavigate = defaultNavigate }: NoVaultPageProps) {
  const { t } = useI18n();
  const [signingOut, setSigningOut] = useState(false);

  async function handleSignOut() {
    setSigningOut(true);
    try {
      await logout();
    } catch {
      // Still leave the session UI even if logout fails network-side.
    }
    onNavigate("/login");
  }

  return (
    <div className="grid min-h-svh place-items-center bg-canvas px-4 text-ink">
      <main className="my-[30px] w-[min(440px,calc(100%-32px))]">
        <AuthCard>
          <p className="text-xs font-extrabold tracking-[0.16em] text-green uppercase">
            {t("ui.product_name")}
          </p>
          <h1 className="mt-2 text-[27px] font-bold tracking-tight">
            {t("no_vault.title")}
          </h1>
          <p className="mt-2 text-sm text-muted">{t("no_vault.subtitle")}</p>
          <div className="mt-6 flex flex-wrap items-center gap-2.5">
            <a
              href="/vaults/new"
              className="inline-flex min-h-11 items-center justify-center rounded-lg bg-primary px-[15px] py-2.5 text-sm font-bold text-primary-foreground"
            >
              {t("no_vault.create")}
            </a>
            <Button
              type="button"
              variant="secondary"
              disabled={signingOut}
              onClick={() => void handleSignOut()}
            >
              {t("ui.sign_out")}
            </Button>
          </div>
        </AuthCard>
      </main>
    </div>
  );
}
