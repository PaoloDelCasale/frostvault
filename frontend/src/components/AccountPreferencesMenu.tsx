import { useContext, useState } from "react";

import { Dialog } from "@/components/Dialog";
import { ThemeControl } from "@/components/ThemeControl";
import { Button } from "@/components/ui/button";
import { I18nContext } from "@/i18n/context";
import { cn } from "@/lib/utils";

const PREFERENCE_KEYS = {
  open: ["ui", "open_preferences"].join("."),
  title: ["ui", "preferences"].join("."),
  description: ["ui", "preferences_description"].join("."),
  close: ["ui", "close_preferences"].join("."),
} as const;

const FALLBACK_LABELS = {
  en: {
    open: "Open preferences",
    title: "Preferences",
    description: "Personal appearance settings for this browser.",
    close: "Close preferences",
  },
  it: {
    open: "Apri preferenze",
    title: "Preferenze",
    description: "Impostazioni personali di aspetto per questo browser.",
    close: "Chiudi preferenze",
  },
} as const;

function translatedLabel(
  key: string,
  value: string,
  fallback: string,
): string {
  return value === key ? fallback : value;
}

type AccountPreferencesMenuProps = {
  className?: string;
  locale?: string;
  t?: (key: string) => string;
};

/**
 * Secondary personal-preferences entry point for authenticated surfaces.
 * Appearance lives here rather than in primary Vault navigation or page bodies.
 */
export function AccountPreferencesMenu({
  className,
  locale: localeProp,
  t: tProp,
}: AccountPreferencesMenuProps) {
  const i18n = useContext(I18nContext);
  const locale = localeProp ?? i18n?.locale ?? "en";
  const t = tProp ?? i18n?.t ?? ((key: string) => key);
  const fallback = locale === "it" ? FALLBACK_LABELS.it : FALLBACK_LABELS.en;
  const [open, setOpen] = useState(false);

  const openLabel = translatedLabel(
    PREFERENCE_KEYS.open,
    t(PREFERENCE_KEYS.open),
    fallback.open,
  );
  const title = translatedLabel(
    PREFERENCE_KEYS.title,
    t(PREFERENCE_KEYS.title),
    fallback.title,
  );
  const description = translatedLabel(
    PREFERENCE_KEYS.description,
    t(PREFERENCE_KEYS.description),
    fallback.description,
  );
  const closeLabel = translatedLabel(
    PREFERENCE_KEYS.close,
    t(PREFERENCE_KEYS.close),
    fallback.close,
  );

  return (
    <>
      <Button
        type="button"
        variant="secondary"
        className={cn("min-h-11", className)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={openLabel}
        data-testid="account-preferences-trigger"
        onClick={() => setOpen(true)}
      >
        {title}
      </Button>

      <Dialog
        open={open}
        onOpenChange={setOpen}
        title={title}
        description={description}
        closeLabel={closeLabel}
        className="w-[min(24rem,calc(100%-1.5rem))]"
      >
        <div data-testid="account-preferences-panel">
          <ThemeControl t={t} locale={locale} className="max-w-full" />
        </div>
      </Dialog>
    </>
  );
}
