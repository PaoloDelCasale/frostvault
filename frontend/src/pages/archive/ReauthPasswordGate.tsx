import type { ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";

import { configureApiClient } from "@/api";
import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

type PasswordWaiter = {
  resolve: (value: string | null) => void;
  reject: (reason: Error) => void;
};

const PASSWORD_PROMPT_CANCELLED = "Reauthentication password prompt was cancelled.";

function passwordPromptCancelledError(): Error {
  return new Error(PASSWORD_PROMPT_CANCELLED);
}

/**
 * Wires `configureApiClient({ requestPassword })` to an accessible dialog so
 * Reauthentication never uses `window.prompt`. A prompt fans its result out
 * to every waiter; the API client separately coalesces its POST submission.
 */
export function ReauthPasswordGate({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const mountedRef = useRef(false);
  const waitersRef = useRef(new Set<PasswordWaiter>());

  const resolveWaiters = useCallback((value: string | null) => {
    const waiters = [...waitersRef.current];
    waitersRef.current.clear();
    if (mountedRef.current) {
      setOpen(false);
      setPassword("");
    }
    for (const waiter of waiters) waiter.resolve(value);
  }, []);

  const rejectWaiters = useCallback(() => {
    const waiters = [...waitersRef.current];
    waitersRef.current.clear();
    if (mountedRef.current) {
      setOpen(false);
      setPassword("");
    }
    const error = passwordPromptCancelledError();
    for (const waiter of waiters) waiter.reject(error);
  }, []);

  const requestPassword = useCallback(
    () =>
      new Promise<string | null>((resolve, reject) => {
        if (!mountedRef.current) {
          reject(passwordPromptCancelledError());
          return;
        }

        const wasEmpty = waitersRef.current.size === 0;
        waitersRef.current.add({ resolve, reject });
        if (wasEmpty) {
          setPassword("");
          setOpen(true);
        }
      }),
    [],
  );

  useEffect(() => {
    mountedRef.current = true;
    configureApiClient({ requestPassword });
    return () => {
      mountedRef.current = false;
      rejectWaiters();
      configureApiClient({ requestPassword: undefined });
    };
  }, [rejectWaiters, requestPassword]);

  return (
    <>
      {children}
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (!next) rejectWaiters();
        }}
        title={t("ui.reauth_password_title")}
        description={t("ui.reauth_password_description")}
        className="w-[min(28rem,calc(100%-1.75rem))]"
      >
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            resolveWaiters(password || null);
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
              onClick={rejectWaiters}
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
