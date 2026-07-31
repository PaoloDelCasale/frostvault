import { useId, useState, type FormEvent } from "react";

import { FormField, FormInput } from "@/components/FormField";
import { Dialog } from "@/components/Dialog";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

type PasswordDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  submitLabel: string;
  minLength?: number;
  onSubmit: (password: string) => void | Promise<void>;
};

/**
 * Accessible password dialog — never uses window.prompt.
 * The submitted value is cleared from local state immediately after submit.
 */
export function PasswordDialog({
  open,
  onOpenChange,
  title,
  description,
  submitLabel,
  minLength = 12,
  onSubmit,
}: PasswordDialogProps) {
  const { t } = useI18n();
  const id = useId();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (password.length < minLength) {
      setError(t("admin.password_min_length"));
      return;
    }
    const value = password;
    setPassword("");
    setError("");
    setBusy(true);
    try {
      await onSubmit(value);
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setPassword("");
          setError("");
        }
        onOpenChange(next);
      }}
      title={title}
      description={description}
      className="w-[min(28rem,calc(100%-1.75rem))]"
    >
      <form className="grid gap-4" onSubmit={(e) => void handleSubmit(e)}>
        <FormField label={t("login.password")} htmlFor={`${id}-password`}>
          <FormInput
            id={`${id}-password`}
            name="password"
            type="password"
            autoComplete="new-password"
            minLength={minLength}
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </FormField>
        {error ? (
          <p role="alert" className="text-sm font-bold text-[var(--state-local-fg)]">
            {error}
          </p>
        ) : null}
        <div className="flex flex-wrap justify-end gap-2">
          <Button
            type="button"
            variant="secondary"
            disabled={busy}
            onClick={() => onOpenChange(false)}
          >
            {t("admin.cancel")}
          </Button>
          <Button type="submit" variant="primary" disabled={busy}>
            {submitLabel}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
