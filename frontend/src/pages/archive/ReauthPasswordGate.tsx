import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";

import { configureApiClient } from "@/api";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

/**
 * Wires `configureApiClient({ requestPassword })` to an accessible dialog so
 * Reauthentication never uses `window.prompt`.
 */
export function ReauthPasswordGate({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const resolverRef = useRef<((value: string | null) => void) | null>(null);

  useEffect(() => {
    configureApiClient({
      requestPassword: () =>
        new Promise<string | null>((resolve) => {
          resolverRef.current = resolve;
          setPassword("");
          setOpen(true);
        }),
    });
    return () => {
      configureApiClient({ requestPassword: undefined });
    };
  }, []);

  function finish(value: string | null) {
    const resolve = resolverRef.current;
    resolverRef.current = null;
    setOpen(false);
    setPassword("");
    resolve?.(value);
  }

  return (
    <>
      {children}
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (!next) finish(null);
        }}
        title={t("ui.reauth_password_title")}
        description={t("ui.reauth_password_description")}
        className="w-[min(28rem,calc(100%-1.75rem))]"
      >
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            finish(password || null);
          }}
        >
          <FormField label={t("login.password")} htmlFor="reauth-password">
            <FormInput
              id="reauth-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              data-testid="reauth-password-input"
            />
          </FormField>
          <div className="flex flex-wrap justify-end gap-2">
            <Button
              type="button"
              variant="secondary"
              className="min-h-11 min-w-11"
              onClick={() => finish(null)}
            >
              {t("ui.cancel")}
            </Button>
            <Button type="submit" variant="primary" className="min-h-11 min-w-11">
              {t("ui.reauth_password_submit")}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
