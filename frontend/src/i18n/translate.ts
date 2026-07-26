/** Lookup + `{name}` interpolation for server-served i18n catalogs (ADR-0006). */

export type MessageParams = Record<string, unknown>;

export function translate(
  messages: Record<string, string>,
  key: string,
  params: MessageParams = {},
): string {
  const raw = messages[key];
  const message = raw === undefined || raw === null ? key : String(raw);
  return message.replace(/\{(\w+)\}/g, (match, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name])
      : match,
  );
}
