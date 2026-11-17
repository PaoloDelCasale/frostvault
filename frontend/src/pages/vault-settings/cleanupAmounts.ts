/** Wait and cap amounts of the automatic cloud cleanups (#726, #771). */
export const GIB = 1024 ** 3;
/**
 * Numbers always grouped, four-digit ones too (#729): «5.000» / «5,000», as
 * the server writes them in this feature's notifications.
 */
export const numberFormat = (locale: string, digits = 0) => {
  const options = { maximumFractionDigits: digits, useGrouping: true };
  try { return new Intl.NumberFormat(locale, options); } catch { return new Intl.NumberFormat("en", options); }
};
export const whole = (value: string) => (/^\d+$/.test(value.trim()) ? Number(value) : NaN);
/**
 * A number typed with or without the locale's digit grouping («1.024,5» in
 * Italian, «1,024.5» in English); a lone `.` or `,` is a decimal separator.
 */
export function parseAmount(value: string, locale: string): number {
  const decimal = numberFormat(locale, 1).formatToParts(1.5).find((part) => part.type === "decimal")?.value ?? ".";
  const group = decimal === "," ? "." : ",";
  const text = value.trim().replace(/[\s\u00a0\u202f']/g, "");
  const grouped = new RegExp(`^\\d{1,3}(\\${group}\\d{3})+(\\${decimal}\\d+)?$`).test(text);
  const plain = (grouped ? text.split(group).join("") : text).replace(",", ".");
  return /^\d+(\.\d+)?$/.test(plain) ? Number(plain) : NaN;
}
