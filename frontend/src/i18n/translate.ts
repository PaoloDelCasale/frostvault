/** Lookup + `{name}` interpolation for server-served i18n catalogs (ADR-0006). */

export type MessageParams = Record<string, unknown>;

export function translate(
  messages: Record<string, string>,
  key: string,
  params: MessageParams = {},
): string {
  const message = messages[key];
  if (message === undefined || message === null) {
    return key;
  }
  return String(message);
}
