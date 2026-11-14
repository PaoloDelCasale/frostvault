import { Check } from "lucide-react";

import { useI18n } from "@/i18n";

/** «Salvato» beside a switch or checkbox that saved on change; see `useSavedFlash`. */
export function SavedIndicator({ visible }: { visible: boolean }) {
  const { t } = useI18n();
  return <span role="status" className="inline-flex min-h-5 items-center gap-1 text-sm font-semibold text-[var(--state-both-fg)]" data-testid="saved-indicator">
    {visible ? <><Check className="size-4" aria-hidden="true" />{t("settings.saved")}</> : null}
  </span>;
}
