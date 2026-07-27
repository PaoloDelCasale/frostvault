import { useEffect, useState, type FormEvent } from "react";

import { Dialog } from "@/components/Dialog";
import { FormField, FormInput } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

export function DisplayNameDialog({
  open,
  onOpenChange,
  initialValue,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialValue: string;
  onSubmit: (displayName: string) => Promise<void>;
}) {
  const { t } = useI18n();
  const [value, setValue] = useState(initialValue);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) {
      setValue(initialValue);
      setError("");
    }
  }, [initialValue, open]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      await onSubmit(value.trim());
      onOpenChange(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={t("admin.edit_display_name_title")}>
      <form className="grid gap-4" onSubmit={(event) => void submit(event)}>
        <FormField label={t("admin.display_name")} htmlFor="admin-edit-display-name">
          <FormInput id="admin-edit-display-name" required value={value} onChange={(event) => setValue(event.target.value)} />
        </FormField>
        {error ? <p role="alert" className="text-sm text-red-700">{error}</p> : null}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>{t("admin.cancel")}</Button>
          <Button type="submit" variant="primary" disabled={saving || !value.trim()}>{t("admin.save_display_name")}</Button>
        </div>
      </form>
    </Dialog>
  );
}
