import { useContext, useEffect, useRef, useState } from "react";

import { Dialog } from "@/components/Dialog";
import { ThemeControl } from "@/components/ThemeControl";
import { Button } from "@/components/ui/button";
import { I18nContext } from "@/i18n/context";
import { cn } from "@/lib/utils";

const MENU_KEYS = {
  open: ["ui", "open_account_menu"].join("."),
  title: ["ui", "account_menu"].join("."),
  description: ["ui", "account_menu_description"].join("."),
  close: ["ui", "close_account_menu"].join("."),
  // Appearance-only surfaces (no-Vault / create) keep the preferences wording.
  openPreferences: ["ui", "open_preferences"].join("."),
  preferencesTitle: ["ui", "preferences"].join("."),
  preferencesDescription: ["ui", "preferences_description"].join("."),
  closePreferences: ["ui", "close_preferences"].join("."),
  newVault: ["ui", "new_vault"].join("."),
  administration: ["ui", "administration"].join("."),
  language: ["ui", "language"].join("."),
  languageEn: ["ui", "language_en"].join("."),
  languageIt: ["ui", "language_it"].join("."),
  signOut: ["ui", "sign_out"].join("."),
} as const;

const FALLBACK_LABELS = {
  en: {
    open: "Open account menu",
    title: "Account",
    description: "Account actions and personal preferences for this browser.",
    close: "Close account menu",
    openPreferences: "Open preferences",
    preferencesTitle: "Preferences",
    preferencesDescription: "Personal appearance settings for this browser.",
    closePreferences: "Close preferences",
    newVault: "New vault",
    administration: "Administration",
    language: "Language",
    languageEn: "English",
    languageIt: "Italiano",
    signOut: "Sign out",
  },
  it: {
    open: "Apri menu account",
    title: "Account",
    description: "Azioni dell’account e preferenze personali per questo browser.",
    close: "Chiudi menu account",
    openPreferences: "Apri preferenze",
    preferencesTitle: "Preferenze",
    preferencesDescription: "Impostazioni personali di aspetto per questo browser.",
    closePreferences: "Chiudi preferenze",
    newVault: "Nuovo vault",
    administration: "Amministrazione",
    language: "Lingua",
    languageEn: "English",
    languageIt: "Italiano",
    signOut: "Esci",
  },
} as const;

const LOCALE_OPTION_KEYS: Record<string, { key: string; fallbackKey: "languageEn" | "languageIt" }> = {
  en: { key: MENU_KEYS.languageEn, fallbackKey: "languageEn" },
  it: { key: MENU_KEYS.languageIt, fallbackKey: "languageIt" },
};

function translatedLabel(
  key: string,
  value: string,
  fallback: string,
): string {
  return value === key ? fallback : value;
}

export type AccountMenuHandlers = {
  onNewVault?: () => void;
  onAdministration?: () => void;
  onSignOut?: () => void;
  onLocaleChange?: (locale: string) => void;
};

type AccountPreferencesMenuProps = {
  className?: string;
  locale?: string;
  locales?: string[];
  /** Fail-closed: Administration is shown only when this flag is true. */
  isAdmin?: boolean;
  /**
   * When provided, the menu becomes the shell secondary surface (New vault,
   * Administration, Language, Appearance, Sign out). Without handlers the
   * menu stays appearance-only for no-Vault / create screens.
   */
  handlers?: AccountMenuHandlers;
  t?: (key: string) => string;
};

/**
 * Account / preferences entry point for authenticated surfaces.
 * Appearance always lives here. Shell secondary destinations are optional.
 */
export function AccountPreferencesMenu({
  className,
  locale: localeProp,
  locales,
  isAdmin = false,
  handlers,
  t: tProp,
}: AccountPreferencesMenuProps) {
  const i18n = useContext(I18nContext);
  const locale = localeProp ?? i18n?.locale ?? "en";
  const t = tProp ?? i18n?.t ?? ((key: string) => key);
  const fallback = locale === "it" ? FALLBACK_LABELS.it : FALLBACK_LABELS.en;
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const wasOpenRef = useRef(false);

  // Controlled Dialog (no Radix Trigger): restore focus like NotificationCenter.
  useEffect(() => {
    if (wasOpenRef.current && !open) {
      triggerRef.current?.focus();
    }
    wasOpenRef.current = open;
  }, [open]);

  const shellSecondary = Boolean(handlers);
  const availableLocales = locales ?? i18n?.locales ?? ["en", "it"];

  const openLabel = translatedLabel(
    shellSecondary ? MENU_KEYS.open : MENU_KEYS.openPreferences,
    t(shellSecondary ? MENU_KEYS.open : MENU_KEYS.openPreferences),
    shellSecondary ? fallback.open : fallback.openPreferences,
  );
  const title = translatedLabel(
    shellSecondary ? MENU_KEYS.title : MENU_KEYS.preferencesTitle,
    t(shellSecondary ? MENU_KEYS.title : MENU_KEYS.preferencesTitle),
    shellSecondary ? fallback.title : fallback.preferencesTitle,
  );
  const description = translatedLabel(
    shellSecondary ? MENU_KEYS.description : MENU_KEYS.preferencesDescription,
    t(shellSecondary ? MENU_KEYS.description : MENU_KEYS.preferencesDescription),
    shellSecondary ? fallback.description : fallback.preferencesDescription,
  );
  const closeLabel = translatedLabel(
    shellSecondary ? MENU_KEYS.close : MENU_KEYS.closePreferences,
    t(shellSecondary ? MENU_KEYS.close : MENU_KEYS.closePreferences),
    shellSecondary ? fallback.close : fallback.closePreferences,
  );

  const newVaultLabel = translatedLabel(
    MENU_KEYS.newVault,
    t(MENU_KEYS.newVault),
    fallback.newVault,
  );
  const administrationLabel = translatedLabel(
    MENU_KEYS.administration,
    t(MENU_KEYS.administration),
    fallback.administration,
  );
  const languageLabel = translatedLabel(
    MENU_KEYS.language,
    t(MENU_KEYS.language),
    fallback.language,
  );
  const signOutLabel = translatedLabel(
    MENU_KEYS.signOut,
    t(MENU_KEYS.signOut),
    fallback.signOut,
  );

  const actionClass =
    "min-h-11 w-full justify-start rounded-[10px] border border-input bg-surface px-4 text-left font-bold text-ink";
  const selectClass =
    "min-h-11 w-full rounded-[10px] border border-input bg-surface px-3 text-ink";

  function runAndClose(action?: () => void) {
    setOpen(false);
    action?.();
  }

  return (
    <>
      <Button
        ref={triggerRef}
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
        <div
          data-testid="account-preferences-panel"
          className="flex flex-col gap-3"
        >
          {shellSecondary ? (
            <>
              <Button
                type="button"
                variant="secondary"
                className={actionClass}
                onClick={() => runAndClose(handlers?.onNewVault)}
              >
                {newVaultLabel}
              </Button>

              {isAdmin ? (
                <Button
                  type="button"
                  variant="secondary"
                  className={actionClass}
                  onClick={() => runAndClose(handlers?.onAdministration)}
                >
                  {administrationLabel}
                </Button>
              ) : null}

              <label className="flex min-h-11 flex-col justify-center gap-1 text-sm font-bold text-muted">
                <span>{languageLabel}</span>
                <select
                  aria-label={languageLabel}
                  className={selectClass}
                  value={locale}
                  onChange={(event) =>
                    handlers?.onLocaleChange?.(event.target.value)
                  }
                >
                  {availableLocales.map((code) => {
                    const option = LOCALE_OPTION_KEYS[code];
                    if (!option) {
                      return (
                        <option key={code} value={code}>
                          {code}
                        </option>
                      );
                    }
                    return (
                      <option key={code} value={code}>
                        {translatedLabel(
                          option.key,
                          t(option.key),
                          fallback[option.fallbackKey],
                        )}
                      </option>
                    );
                  })}
                </select>
              </label>
            </>
          ) : null}

          <ThemeControl t={t} locale={locale} className="max-w-full" />

          {shellSecondary ? (
            <Button
              type="button"
              variant="secondary"
              className={actionClass}
              onClick={() => runAndClose(handlers?.onSignOut)}
            >
              {signOutLabel}
            </Button>
          ) : null}
        </div>
      </Dialog>
    </>
  );
}
