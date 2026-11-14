/**
 * Lookup + `{name}` interpolation for server-served i18n catalogs (ADR-0006).
 *
 * Counted strings: a numeric `count` param selects `<key>_<category>` with
 * `Intl.PluralRules` (`_one`, `_other`, …), then `<key>_other`, then `<key>`.
 * A numeric `count` is shown with the locale's digit grouping.
 */

export type MessageParams = Record<string, unknown>;

function pluralKey(messages: Record<string, string>, key: string, count: number, locale: string): string {
  let category = "other";
  try {
    category = new Intl.PluralRules(locale).select(count);
  } catch {
    category = count === 1 ? "one" : "other";
  }
  for (const candidate of [`${key}_${category}`, `${key}_other`]) {
    if (messages[candidate] !== undefined && messages[candidate] !== null) return candidate;
  }
  return key;
}

function formatCount(count: number, locale: string): string {
  try {
    return new Intl.NumberFormat(locale).format(count);
  } catch {
    return String(count);
  }
}

export function translate(
  messages: Record<string, string>,
  key: string,
  params: MessageParams = {},
  locale = "en",
): string {
  const count = params.count;
  const counted = typeof count === "number" && Number.isFinite(count);
  const resolved = counted ? pluralKey(messages, key, count, locale) : key;
  const raw = messages[resolved];
  const message = raw === undefined || raw === null ? key : String(raw);
  return message.replace(/\{(\w+)\}/g, (match, name: string) => {
    if (!Object.prototype.hasOwnProperty.call(params, name)) return match;
    return counted && name === "count" ? formatCount(count, locale) : String(params[name]);
  });
}
