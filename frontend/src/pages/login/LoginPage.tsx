import { useState, type FormEvent } from "react";

import { ApiError, loginWithPassword } from "@/api/client";
import { AuthCard } from "@/components/AuthCard";
import { FormField, FormInput, FormSelect } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

type LoginPageProps = {
  /** Navigation after successful Break-glass Login (defaults to location.assign). */
  onNavigate?: (url: string) => void;
};

function defaultNavigate(url: string): void {
  window.location.assign(url);
}

export function LoginPage({ onNavigate = defaultNavigate }: LoginPageProps) {
  const { t, locale, locales, setLocale } = useI18n();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await loginWithPassword(username, password);
      onNavigate("/");
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setError(t("login.local_unavailable"));
      } else {
        setError(t("login.failed"));
      }
      setSubmitting(false);
    }
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
              onClick={() => onNavigate("/auth/oidc/login")}
            >
              {t("login.oidc")}
            </Button>
          </div>

          <label className="mt-5 inline-flex min-h-11 items-center gap-2 text-sm font-bold text-muted">
            <span>{t("ui.language")}</span>
            <FormSelect
              aria-label={t("ui.language")}
              value={locale}
              onChange={(event) => {
                void setLocale(event.target.value);
              }}
            >
              {locales.map((code) => (
                <option key={code} value={code}>
                  {t(`ui.language_${code}`)}
                </option>
              ))}
            </FormSelect>
          </label>
        </AuthCard>
      </main>
    </div>
  );
}
