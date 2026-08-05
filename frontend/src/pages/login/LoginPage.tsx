import { useEffect, useState, type FormEvent } from "react";

import { fetchMe } from "@/api";
import { ApiError, loginWithPassword } from "@/api/client";
import { AuthCard } from "@/components/AuthCard";
import { ThemeControl } from "@/components/ThemeControl";
import { FormField, FormInput, FormSelect } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";
import { useTheme } from "@/theme";
import {
  beginOfflineFileCacheTransition,
  finishOfflineFileCacheTransition,
  isOfflineCacheContext,
  prepareOfflineFileCacheContext,
  setOfflineFileCacheContext,
} from "@/pwa/offlineFiles";

const LOGIN_TRANSITION_TIMEOUT_MS = 5_000;

class LoginTransitionTimeoutError extends Error {
  constructor() {
    super("Login transition request timed out");
  }
}

function withinLoginTransitionTimeout<T>(work: Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      callback();
    };
    const timeout = setTimeout(
      () => finish(() => reject(new LoginTransitionTimeoutError())),
      LOGIN_TRANSITION_TIMEOUT_MS,
    );
    void work.then(
      (value) => finish(() => resolve(value)),
      (error: unknown) => finish(() => reject(error)),
    );
  });
}

type LoginPageProps = {
  /** Navigation after successful local sign-in (defaults to location.assign). */
  onNavigate?: (url: string) => void;
};

function defaultNavigate(url: string): void {
  window.location.assign(url);
}

export function LoginPage({ onNavigate = defaultNavigate }: LoginPageProps) {
  const { t, locale, locales, setLocale } = useI18n();
  const { setUserId } = useTheme();

  useEffect(() => {
    // A login screen has no trusted identity. Never reuse a previous user's override.
    setUserId(null);
  }, [setUserId]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    const transition = await beginOfflineFileCacheTransition();
    try {
      await withinLoginTransitionTimeout(loginWithPassword(username, password));
    } catch (err) {
      if (!(err instanceof LoginTransitionTimeoutError)) {
        // A rejected login leaves the prior server Session authoritative. It
        // may reopen only after a new /api/me; otherwise the Worker remains
        // closed/network-only for the next attempt.
        await (async () => {
          const freshness = await prepareOfflineFileCacheContext();
          const me = await withinLoginTransitionTimeout(fetchMe());
          const context = me.vault ? { userId: me.id, vaultId: me.vault.id } : null;
          if (context && isOfflineCacheContext(context)) {
            await setOfflineFileCacheContext(context, freshness, transition);
          } else {
            await finishOfflineFileCacheTransition(transition, freshness);
          }
        })().catch(() => undefined);
      }
      if (err instanceof ApiError && err.status === 403) {
        setError(t("login.local_unavailable"));
      } else {
        setError(t("login.failed"));
      }
      setSubmitting(false);
      return;
    }

    try {
      // The Worker remains closed across login. This response is the first
      // post-mutation authority allowed to register/reopen its cache context.
      const freshness = await prepareOfflineFileCacheContext();
      const me = await withinLoginTransitionTimeout(fetchMe());
      const context = me.vault ? { userId: me.id, vaultId: me.vault.id } : null;
      if (context && isOfflineCacheContext(context)) {
        await setOfflineFileCacheContext(context, freshness, transition);
      } else {
        await finishOfflineFileCacheTransition(transition, freshness);
      }
      setUserId(me.id);
    } catch {
      // Authentication succeeded. Keep the first paint identity-safe and let
      // the destination retry /api/me. A failed reconciliation stays closed.
      setUserId(null);
    }
    onNavigate("/");
  }

  return (
    <div className="grid min-h-svh place-items-center bg-canvas px-4 text-ink">
      <main className="my-[30px] w-[min(440px,calc(100%-32px))]">
        <AuthCard>
          <p className="text-xs font-extrabold tracking-[0.16em] text-green uppercase">
            {t("ui.product_name")}
          </p>
          <h1 className="mt-2 text-[27px] font-bold tracking-tight">
            {t("login.welcome")}
          </h1>
          <p className="mt-2 text-sm text-muted">{t("login.subtitle")}</p>
          <p className="mt-3 text-sm text-muted">{t("login.admin_recovery")}</p>

          <form className="mt-6 grid gap-3.5" onSubmit={(e) => void handleSubmit(e)}>
            <FormField label={t("login.username")} htmlFor="login-username">
              <FormInput
                id="login-username"
                name="username"
                autoComplete="username"
                required
                autoFocus
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
            </FormField>
            <FormField label={t("login.password")} htmlFor="login-password">
              <FormInput
                id="login-password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </FormField>
            {error ? (
              <div
                className="rounded-[10px] bg-red-soft px-3.5 py-3 text-sm text-ink"
                role="alert"
              >
                {error}
              </div>
            ) : null}
            <Button type="submit" variant="primary" disabled={submitting}>
              {t("login.submit")}
            </Button>
          </form>

          <div className="mt-4">
            <Button
              type="button"
              variant="secondary"
              className="w-full"
              onClick={() => {
                // OIDC does not identify the next user yet; never carry this
                // session's marker across the authentication redirect.
                void beginOfflineFileCacheTransition();
                setUserId(null);
                onNavigate("/auth/oidc/login");
              }}
            >
              {t("login.oidc")}
            </Button>
          </div>

          <div className="mt-5 grid gap-3 sm:grid-cols-2">
            <label className="flex min-h-11 flex-col justify-center gap-1 text-sm font-bold text-muted">
              <span>{t("ui.language")}</span>
              <FormSelect
                aria-label={t("ui.language")}
                value={locale}
                onChange={(event) => {
                  void setLocale(event.target.value, { mode: "guest" });
                }}
              >
                {locales.map((code) => (
                  <option key={code} value={code}>
                    {t(`ui.language_${code}`)}
                  </option>
                ))}
              </FormSelect>
            </label>
            <ThemeControl />
          </div>
        </AuthCard>
      </main>
    </div>
  );
}
