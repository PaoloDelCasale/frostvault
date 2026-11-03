import { useState } from "react";

import { logout } from "@/api/endpoints";
import { AccountPreferencesMenu } from "@/components/AccountPreferencesMenu";
import { AuthCard } from "@/components/AuthCard";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";
import { useTheme } from "@/theme";

type NoVaultPageProps = {
  onNavigate?: (url: string) => void;
};

function defaultNavigate(url: string): void {
  window.location.assign(url);
}

export function NoVaultPage({ onNavigate = defaultNavigate }: NoVaultPageProps) {
  const { t } = useI18n();
  const { setUserId } = useTheme();
  const [signingOut, setSigningOut] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSignOut() {
    setSigningOut(true);
    setError(null);
    try {
      await logout();
      setUserId(null);
      onNavigate("/login");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSigningOut(false);
    }
  }

  return (
    <div className="relative grid min-h-svh place-items-center bg-canvas px-4 text-ink">
      <div className="absolute top-[max(0.75rem,env(safe-area-inset-top))] right-[max(1rem,env(safe-area-inset-right))]">
        <AccountPreferencesMenu />
      </div>
      <main className="my-[30px] w-[min(440px,calc(100%-32px))]">
        <AuthCard>
          <p className="text-xs font-extrabold tracking-[0.16em] text-green uppercase">
            {t("ui.product_name")}
          </p>
          <h1 className="mt-2 text-[27px] font-bold tracking-tight">
            {t("no_vault.title")}
          </h1>
          <p className="mt-2 text-sm text-muted">{t("no_vault.subtitle")}</p>
          {error ? <p role="alert">{error}</p> : null}
          <div className="mt-6 flex flex-wrap items-center gap-2.5">
            <a
              href="/vaults/new"
              className="inline-flex min-h-11 items-center justify-center rounded-lg bg-primary px-[15px] py-2.5 text-sm font-bold text-primary-foreground transition-[box-shadow,filter,transform] duration-150 hover:-translate-y-px hover:brightness-105 hover:shadow-[0_5px_14px_var(--interactive-shadow-hover)] focus-visible:ring-3 focus-visible:ring-ring/50 motion-reduce:transition-none"
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
