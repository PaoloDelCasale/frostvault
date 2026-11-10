export type ShellTranslator = (key: string, params?: Record<string, unknown>) => string;

const fill = (text: string, params?: Record<string, unknown>) =>
  Object.entries(params ?? {}).reduce((out, [name, value]) => out.replaceAll(`{${name}}`, String(value)), text);

/** Keep isolated shell stories/tests usable without mounting the i18n provider. */
export function shellLabel(
  t: ShellTranslator | undefined,
  key: string,
  fallback: string,
  params?: Record<string, unknown>,
): string {
  const translated = t?.(key, params);
  return fill(translated && translated !== key ? translated : fallback, params);
}
