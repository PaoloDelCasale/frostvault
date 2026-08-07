import { useState } from "react";

import { logout } from "@/api/endpoints";
import { AccountPreferencesMenu } from "@/components/AccountPreferencesMenu";
import { AuthCard } from "@/components/AuthCard";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";
import { useTheme } from "@/theme";
import {
  AuthTransitionTimeoutError,
  beginOfflineAuthTransition,
  reconcileOfflineAuthTransition,
  withinAuthTransitionTimeout,
} from "@/pwa/authTransition";

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

  async function handleSignOut() {
    // No-Vault still owns a live Session, so it must use the same bounded
    // transition as the archive sign-out path before navigating away.
    setSigningOut(true);
    const transition = await beginOfflineAuthTransition();
    setUserId(null);
    try {
      await withinAuthTransitionTimeout(logout());
      // A successful logout deliberately leaves the cache barrier closed.
    } catch (error) {
      if (!(error instanceof AuthTransitionTimeoutError)) {
        // A known failure can reopen only after the shared fresh /api/me path.
        await reconcileOfflineAuthTransition({ transition }).catch(() => undefined);
      }
    }
    onNavigate("/login");
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
