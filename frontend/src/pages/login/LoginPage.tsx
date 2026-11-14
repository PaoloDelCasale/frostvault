import { useEffect, useState, type FormEvent } from "react";

import { ApiError, loginWithPassword } from "@/api/client";
import { fetchLoginOptions } from "@/api/endpoints";
import { AuthCard } from "@/components/AuthCard";
import { ThemeControl } from "@/components/ThemeControl";
import { FormField, FormInput, FormSelect } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";
import { useTheme } from "@/theme";

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
    // The login screen has no authenticated Owner theme.
    setUserId(null);
  }, [setUserId]);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [redirectingOidc, setRedirectingOidc] = useState(false);
  // null while loading; `oidc: null` when the options could not be read.
  const [options, setOptions] = useState<{ oidc: boolean | null; name: string | null } | null>(null);

  useEffect(() => {
    let live = true;
    void fetchLoginOptions()
      .then((data) => { if (live) setOptions({ oidc: data.oidc_enabled, name: data.oidc_provider_name ?? null }); })
      .catch(() => { if (live) setOptions({ oidc: null, name: null }); });
    return () => { live = false; };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await loginWithPassword(password);
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

  // OIDC is the normal sign-in when configured; unknown options keep both methods.
  const oidcFirst = options?.oidc === true;
  const oidcLabel = options?.name ? t("login.oidc_named", { name: options.name }) : t("login.oidc");

  function oidcButton(variant: "primary" | "secondary") {
    return (
      <Button
        type="button"
        variant={variant}
        className="w-full"
        disabled={submitting || redirectingOidc}
        onClick={() => {
          setRedirectingOidc(true);
          onNavigate("/auth/oidc/login");
        }}
      >
        {oidcLabel}
      </Button>
    );
  }

  function passwordForm(variant: "primary" | "secondary") {
    return (
      <form className="mt-4 grid gap-3.5" onSubmit={(e) => void handleSubmit(e)}>
        <FormField label={t("login.password")} htmlFor="login-password">
          <FormInput
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            autoFocus={variant === "primary"}
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
        <Button type="submit" variant={variant} disabled={submitting}>
          {t(variant === "primary" ? "login.submit" : "login.emergency_submit")}
        </Button>
      </form>
    );
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
          {options && (oidcFirst ? (
            <>
              <div className="mt-6">{oidcButton("primary")}</div>
              <section className="mt-6 border-t border-line pt-5" aria-labelledby="login-emergency-title">
                <h2 id="login-emergency-title" className="text-sm font-bold">{t("login.emergency_title")}</h2>
                <p className="mt-1 text-sm text-muted">{t("login.emergency_help")}</p>
                {passwordForm("secondary")}
              </section>
            </>
          ) : (
            <>
              <p className="mt-2 text-sm text-muted">{t("login.password_help")}</p>
              {passwordForm("primary")}
              {options.oidc === null && <div className="mt-4">{oidcButton("secondary")}</div>}
            </>
          ))}

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
