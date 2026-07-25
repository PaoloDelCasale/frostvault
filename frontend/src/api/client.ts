import type { AuthMethod } from "./types";

const CSRF_COOKIE_NAME = "frostvault_csrf";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const SIGN_IN_PATH = "/login";

export type ApiFetch = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export type ApiClientConfig = {
  fetch?: ApiFetch;
  csrfCookieName?: string;
  /** Cached CSRF from /api/me; cookie is used when this is empty. */
  csrfToken?: string | null;
  getAuthMethod?: () => AuthMethod | undefined;
  /** Break-glass Login password dialog. Return null to cancel. */
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

export function configureApiClient(next: ApiClientConfig): void {
  config = { ...config, ...next };
}

export function resetApiClientForTests(): void {
  config = {};
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

async function stepUpReauthentication(): Promise<boolean> {
  const authMethod = config.getAuthMethod?.();
  if (authMethod === "oidc") {
    const returnTo = encodeURIComponent(currentReturnTo());
    navigateTo(`/auth/oidc/reauth?return_to=${returnTo}`);
    throw new ReauthenticationRedirectError();
  }

  const password = await (config.requestPassword?.() ?? Promise.resolve(null));
  if (!password) {
    throw new ApiError("Reauthentication required for this action.", {
      status: 403,
      messageKey: "ui.reauth_failed",
    });
  }

  const headers = new Headers({
    "Content-Type": "application/json",
    "X-CSRF-Token": resolveCsrfToken(),
  });
  const response = await resolveFetch()("/api/reauth", {
    method: "POST",
    headers,
    body: JSON.stringify({ password }),
  });
  if (!response.ok) {
    const key = "ui.reauth_failed";
    const message = config.translate?.(key) ?? "Reauthentication failed.";
    throw new ApiError(message, { status: response.status, messageKey: key });
  }
  return true;
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

export async function apiRequest<T = unknown>(
  url: string,
  options: RequestInit = {},
  allowReauthRetry = true,
): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = new Headers(options.headers);
  if (!headers.has("Content-Type") && options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (MUTATING_METHODS.has(method)) {
    headers.set("X-CSRF-Token", resolveCsrfToken());
  }

  const response = await resolveFetch()(url, { ...options, method, headers });
  const data = await parseBody(response);

  if (response.status === 401) {
    navigateTo(SIGN_IN_PATH);
    throw new ApiError("Authentication required.", { status: 401, body: data });
  }

  if (isReauthRequired(response.status, data) && allowReauthRetry) {
    await stepUpReauthentication();
    return apiRequest<T>(url, options, false);
  }

  if (!response.ok) {
    const { message, messageKey } = errorMessageFromBody(data, response.status);
    throw new ApiError(message, {
      status: response.status,
      messageKey,
      body: data,
    });
  }

  return data as T;
}
