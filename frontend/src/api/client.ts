import type { AuthMethod } from "./types";

const CSRF_COOKIE_NAME = "frostvault_csrf";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const SIGN_IN_PATH = "/login";

export type ApiFetch = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export type ApiDownload = {
  blob: Blob;
  filename: string;
  /** Normalized X-Checksum-SHA256, or null when the server omitted/invalidated it. */
  checksumSha256: string | null;
};

export type ApiClientConfig = {
  fetch?: ApiFetch;
  csrfCookieName?: string;
  /** Cached CSRF from /api/me; cookie is used when this is empty. */
  csrfToken?: string | null;
  getAuthMethod?: () => AuthMethod | undefined;
  /** Break-glass Login password dialog. Return null or reject to cancel. */
  requestPassword?: () => Promise<string | null>;
  /** Navigation for OIDC Reauthentication and 401 → sign-in. */
  navigate?: (url: string) => void;
  /** Resolve message_key to a localized string. */
  translate?: (key: string, params?: Record<string, unknown>) => string;
  getPathname?: () => string;
  getSearch?: () => string;
};

/** Raised when OIDC Reauthentication navigation starts; not a silent failure. */
export class ReauthenticationRedirectError extends Error {
  constructor(message = "Reauthentication redirect started") {
    super(message);
    this.name = "ReauthenticationRedirectError";
  }
}

export class ApiError extends Error {
  readonly status: number;
  readonly messageKey?: string;
  readonly body: unknown;

  constructor(
    message: string,
    options: { status: number; messageKey?: string; body?: unknown },
  ) {
    super(message);
    this.name = "ApiError";
    this.status = options.status;
    this.messageKey = options.messageKey;
    this.body = options.body ?? null;
  }
}

let config: ApiClientConfig = {};
let localReauthenticationInFlight: Promise<boolean> | null = null;

export function configureApiClient(next: ApiClientConfig): void {
  config = { ...config, ...next };
}

export function resetApiClientForTests(): void {
  config = {};
  localReauthenticationInFlight = null;
}

export function setCsrfToken(token: string | null): void {
  config.csrfToken = token;
}

function readCookie(name: string): string {
  if (typeof document === "undefined") return "";
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const cookie = part.trim();
    if (cookie.startsWith(prefix)) {
      return decodeURIComponent(cookie.slice(prefix.length));
    }
  }
  return "";
}

function resolveCsrfToken(): string {
  if (config.csrfToken) return config.csrfToken;
  const cookieName = config.csrfCookieName ?? CSRF_COOKIE_NAME;
  return readCookie(cookieName);
}

function resolveFetch(): ApiFetch {
  return config.fetch ?? fetch;
}

function navigateTo(url: string): void {
  if (config.navigate) {
    config.navigate(url);
    return;
  }
  window.location.href = url;
}

function currentReturnTo(): string {
  const pathname =
    config.getPathname?.() ??
    (typeof window !== "undefined" ? window.location.pathname : "/");
  const search =
    config.getSearch?.() ??
    (typeof window !== "undefined" ? window.location.search : "");
  return `${pathname}${search}`;
}

function isReauthRequired(status: number, data: unknown): boolean {
  return (
    status === 403 &&
    !!data &&
    typeof data === "object" &&
    "error" in data &&
    (data as { error: unknown }).error === "reauth_required"
  );
}

function errorMessageFromBody(
  data: unknown,
  status: number,
): { message: string; messageKey?: string } {
  const translate = config.translate;
  if (data && typeof data === "object") {
    const record = data as Record<string, unknown>;
    if (typeof record.message_key === "string" && translate) {
      return {
        message: translate(
          record.message_key,
          (record.message_params as Record<string, unknown> | undefined) ??
            undefined,
        ),
        messageKey: record.message_key,
      };
    }
    if (typeof record.message === "string" && record.message) {
      return {
        message: record.message,
        messageKey:
          typeof record.message_key === "string"
            ? record.message_key
            : undefined,
      };
    }
    if (typeof record.detail === "string") {
      return { message: record.detail };
    }
    if (Array.isArray(record.detail)) {
      return {
        message: record.detail
          .map((item) =>
            item && typeof item === "object" && "msg" in item
              ? String((item as { msg: unknown }).msg)
              : "Invalid value",
          )
          .join("; "),
      };
    }
    if (typeof record.error === "string" && record.error !== "reauth_required") {
      return { message: record.error };
    }
  }
  const fallback =
    status >= 500
      ? `Internal server error (HTTP ${status})`
      : `Operation failed (HTTP ${status})`;
  return { message: fallback };
}

function reauthenticationFailure(status = 403): ApiError {
  const key = "ui.reauth_failed";
  const message = config.translate?.(key) ?? "Reauthentication failed.";
  return new ApiError(message, { status, messageKey: key });
}

async function submitLocalReauthentication(): Promise<boolean> {
  let password: string | null;
  try {
    password = await (config.requestPassword?.() ?? Promise.resolve(null));
  } catch {
    // The password gate rejects on cancel or unmount. Keep that UI lifecycle
    // detail out of API callers, which consistently receive a displayable
    // reauthentication failure instead.
    throw reauthenticationFailure();
  }
  if (!password) throw reauthenticationFailure();

  const headers = new Headers({
    "Content-Type": "application/json",
    "X-CSRF-Token": resolveCsrfToken(),
  });
  const response = await resolveFetch()("/api/reauth", {
    method: "POST",
    headers,
    body: JSON.stringify({ password }),
  });
  if (!response.ok) throw reauthenticationFailure(response.status);
  return true;
}

function localReauthentication(): Promise<boolean> {
  if (localReauthenticationInFlight) return localReauthenticationInFlight;

  const attempt = submitLocalReauthentication();
  localReauthenticationInFlight = attempt;
  void attempt.then(
    () => {
      if (localReauthenticationInFlight === attempt) {
        localReauthenticationInFlight = null;
      }
    },
    () => {
      if (localReauthenticationInFlight === attempt) {
        localReauthenticationInFlight = null;
      }
    },
  );
  return attempt;
}

async function stepUpReauthentication(): Promise<boolean> {
  const authMethod = config.getAuthMethod?.();
  if (authMethod === "oidc") {
    const returnTo = encodeURIComponent(currentReturnTo());
    navigateTo(`/auth/oidc/reauth?return_to=${returnTo}`);
    throw new ReauthenticationRedirectError();
  }

  return localReauthentication();
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text) as unknown;
  } catch {
    if (response.ok) {
      throw new Error(
        `Invalid response from the server (HTTP ${response.status})`,
      );
    }
    return {};
  }
}

function requestOptions(options: RequestInit): RequestInit & {
  method: string;
  headers: Headers;
} {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = new Headers(options.headers);
  if (!headers.has("Content-Type") && options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (MUTATING_METHODS.has(method)) {
    headers.set("X-CSRF-Token", resolveCsrfToken());
  }
  return { ...options, method, headers };
}

/**
 * Perform an authenticated request while keeping the shared session, CSRF,
 * and reauthentication behavior in one place for both JSON and binary APIs.
 */
async function requestResponse(
  url: string,
  options: RequestInit,
  allowReauthRetry: boolean,
): Promise<Response> {
  const response = await resolveFetch()(url, requestOptions(options));
  if (response.ok) return response;

  const data = await parseBody(response);
  if (response.status === 401) {
    navigateTo(SIGN_IN_PATH);
    throw new ApiError("Authentication required.", { status: 401, body: data });
  }

  if (isReauthRequired(response.status, data) && allowReauthRetry) {
    await stepUpReauthentication();
    return requestResponse(url, options, false);
  }

  const { message, messageKey } = errorMessageFromBody(data, response.status);
  throw new ApiError(message, {
    status: response.status,
    messageKey,
    body: data,
  });
}

export async function apiRequest<T = unknown>(
  url: string,
  options: RequestInit = {},
  allowReauthRetry = true,
): Promise<T> {
  const response = await requestResponse(url, options, allowReauthRetry);
  return (await parseBody(response)) as T;
}

function decodeExtendedFilename(value: string): string {
  const encoded = /^([^']*)'[^']*'(.*)$/.exec(value)?.[2] ?? value;
  try {
    return decodeURIComponent(encoded);
  } catch {
    // A malformed percent escape must not make the entire download fail. The
    // subsequent filename sanitization still prevents path/control injection.
    return encoded;
  }
}

function stripFilenameControlCharacters(value: string): string {
  return Array.from(value, (character) => {
    const code = character.charCodeAt(0);
    return code < 32 || code === 127 ? "_" : character;
  }).join("");
}

function sanitizeDownloadFilename(value: string, fallback: string): string {
  const sanitized = stripFilenameControlCharacters(value)
    .replace(/[\\/<>:"|?*]/g, "_")
    .trim()
    .replace(/^\.+$/, "")
    .slice(0, 255);
  if (sanitized) return sanitized;
  const safeFallback = stripFilenameControlCharacters(fallback)
    .replace(/[\\/<>:"|?*]/g, "_")
    .trim()
    .replace(/^\.+$/, "")
    .slice(0, 255);
  return safeFallback || "download";
}

/** Parse and sanitize RFC 6266/RFC 5987 Content-Disposition filenames. */
export function filenameFromContentDisposition(
  header: string | null,
  fallback = "download",
): string {
  if (!header) return sanitizeDownloadFilename(fallback, "download");

  const extended = /(?:^|;)\s*filename\*\s*=\s*(?:"([^"]*)"|([^;]*))/i.exec(
    header,
  );
  const regular = /(?:^|;)\s*filename\s*=\s*(?:"([^"]*)"|([^;]*))/i.exec(
    header,
  );
  const raw = extended?.[1] ?? extended?.[2] ?? regular?.[1] ?? regular?.[2];
  if (!raw) return sanitizeDownloadFilename(fallback, "download");

  const unquoted = raw.replace(/\\([\\"])/g, "$1");
  const decoded = extended ? decodeExtendedFilename(unquoted) : unquoted;
  return sanitizeDownloadFilename(decoded, fallback);
}

function checksumFromHeader(value: string | null): string | null {
  const checksum = value?.trim().toLowerCase() ?? "";
  return /^[a-f0-9]{64}$/.test(checksum) ? checksum : null;
}

/** Fetch an authenticated binary response without exposing fetch to pages. */
export async function apiDownload(
  url: string,
  options: RequestInit = {},
  fallbackFilename = "download",
): Promise<ApiDownload> {
  const response = await requestResponse(url, options, true);
  return {
    blob: await response.blob(),
    filename: filenameFromContentDisposition(
      response.headers.get("Content-Disposition"),
      fallbackFilename,
    ),
    checksumSha256: checksumFromHeader(
      response.headers.get("X-Checksum-SHA256"),
    ),
  };
}

/**
 * Break-glass Login via POST /api/login.
 *
 * Does not use apiRequest: a 401 for wrong credentials must not navigate away
 * from the sign-in screen (apiRequest sends every 401 to /login).
 */
export async function loginWithPassword(
  username: string,
  password: string,
): Promise<void> {
  const headers = new Headers({ "Content-Type": "application/json" });
  const response = await resolveFetch()("/api/login", {
    method: "POST",
    headers,
    body: JSON.stringify({ username, password }),
  });
  const data = await parseBody(response);
  if (!response.ok) {
    let detail: string | undefined;
    if (data && typeof data === "object" && "detail" in data) {
      const raw = (data as { detail: unknown }).detail;
      if (typeof raw === "string") detail = raw;
    }
    throw new ApiError(detail ?? `Sign-in failed (HTTP ${response.status})`, {
      status: response.status,
      body: data,
    });
  }
}
