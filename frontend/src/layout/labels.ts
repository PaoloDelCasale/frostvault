export type ShellTranslator = (key: string) => string;

/** Keep isolated shell stories/tests usable without mounting the i18n provider. */
export function shellLabel(
  t: ShellTranslator | undefined,
  key: string,
  fallback: string,
): string {
  const translated = t?.(key);
  return translated && translated !== key ? translated : fallback;
}
