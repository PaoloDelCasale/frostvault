import { useContext } from "react";

import { I18nContext } from "@/i18n/context";
import { cn } from "@/lib/utils";
import { useTheme, type ThemePreference } from "@/theme";

const THEME_KEYS = {
  label: ["ui", "theme"].join("."),
  system: ["ui", "theme_system"].join("."),
  light: ["ui", "theme_light"].join("."),
  dark: ["ui", "theme_dark"].join("."),
} as const;

const FALLBACK_LABELS = {
  en: {
    label: "Appearance",
    system: "System",
    light: "Light",
    dark: "Dark",
  },
  it: {
    label: "Aspetto",
    system: "Sistema",
    light: "Chiaro",
    dark: "Scuro",
  },
} as const;

function translatedLabel(
  key: string,
  value: string,
  fallback: string,
): string {
  return value === key ? fallback : value;
}

type ThemeControlProps = {
  className?: string;
  locale?: string;
  t?: (key: string) => string;
};

/**
 * A keyboard-friendly, translated appearance selector. A select is deliberately
 * used instead of a colour-only icon toggle so all three choices are announced
 * by assistive technology and remain usable at 320px wide.
 */
export function ThemeControl({
  className,
  locale: localeProp,
  t: tProp,
}: ThemeControlProps) {
  const i18n = useContext(I18nContext);
  const locale = localeProp ?? i18n?.locale ?? "en";
  const t = tProp ?? i18n?.t ?? ((key: string) => key);
  const { preference, setTheme } = useTheme();
  const fallback = locale === "it" ? FALLBACK_LABELS.it : FALLBACK_LABELS.en;
  const label = translatedLabel(
    THEME_KEYS.label,
    t(THEME_KEYS.label),
    fallback.label,
  );
  const options: Array<[ThemePreference, string, string]> = [
    ["system", THEME_KEYS.system, fallback.system],
    ["light", THEME_KEYS.light, fallback.light],
    ["dark", THEME_KEYS.dark, fallback.dark],
  ];

  return (
    <label className={cn("flex min-h-11 flex-col justify-center gap-1 text-sm font-bold text-muted", className)}>
      <span>{label}</span>
      <select
        aria-label={label}
        className="min-h-11 rounded-[10px] border border-input bg-surface px-3 text-ink"
        value={preference}
        onChange={(event) => setTheme(event.target.value as ThemePreference)}
        data-testid="theme-control"
      >
        {options.map(([value, key, fallbackText]) => (
          <option key={value} value={value}>
            {translatedLabel(key, t(key), fallbackText)}
          </option>
        ))}
      </select>
    </label>
  );
}
