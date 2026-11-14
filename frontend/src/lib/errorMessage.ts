import { ApiError, ReauthenticationRedirectError } from "@/api/client";

export type ErrorTranslate = (
  key: string,
  params?: Record<string, string | number>,
) => string;

function displayParams(
  params: Record<string, unknown> | undefined,
  status: number,
): Record<string, string | number> {
  const out: Record<string, string | number> = { status };
  for (const [name, value] of Object.entries(params ?? {})) {
    if (typeof value === "string" || typeof value === "number") out[name] = value;
    else if (value !== null && value !== undefined) out[name] = String(value);
  }
  return out;
}

/** A catalog lookup that reports a missing key as no translation. */
function lookup(
  t: ErrorTranslate,
  key: string,
  params?: Record<string, string | number>,
): string {
  const text = t(key, params);
  return text && text !== key ? text : "";
}

/**
 * The single way to show a failure to the Owner: the localized message without
 * a technical prefix («ApiError:», «TypeError:»). Fallbacks for HTTP status,
 * network failures and unknown errors come from the i18n catalogs (#651).
 */
export function errorMessage(error: unknown, t: ErrorTranslate): string {
  if (error instanceof ApiError) {
    const params = displayParams(error.messageParams, error.status);
    const keyed = error.messageKey ? lookup(t, error.messageKey, params) : "";
    if (keyed) return keyed;
    if (error.message) return error.message;
    return t(error.status >= 500 ? "ui.error_server" : "ui.error_http", params);
  }
  if (error instanceof ReauthenticationRedirectError) {
    return t("ui.error_reauth_redirect");
  }
  // A failed network request rejects with a TypeError
  // («Failed to fetch», «NetworkError…», «Load failed» depending on browser).
  if (error instanceof TypeError) return t("ui.error_network");
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error) return error;
  return t("ui.error_unknown");
}
