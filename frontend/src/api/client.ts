const CSRF_COOKIE_NAME = "frostvault_csrf";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export type AuthMethod = "oidc" | "local" | string | null | undefined;

export type ApiFetch = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export type ApiClientConfig = {
  fetch?: ApiFetch;
  csrfCookieName?: string;
  /** Cached CSRF from /api/me; cookie is used when this is empty. */
  csrfToken?: string | null;
  getAuthMethod?: () => AuthMethod;
  /** Break-glass Login password dialog. Return null to cancel. */
  requestPassword?: () => Promise<string | null>;
  /** Navigation for OIDC Reauthentication and 401 → sign-in. */
  navigate?: (url: string) => void;
  /** Resolve message_key to a localized string. */
  translate?: (key: string, params?: Record<string, unknown>) => string;
  getPathname?: () => string;
  getSearch?: () => string;
};

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

export async function apiRequest<T = unknown>(
  url: string,
  options: RequestInit = {},
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
  const text = await response.text();
  let data: unknown = {};
  if (text) {
    try {
      data = JSON.parse(text) as unknown;
    } catch {
      if (response.ok) {
        throw new Error(
          `Invalid response from the server (HTTP ${response.status})`,
        );
      }
    }
  }

  if (!response.ok) {
    const fallback =
      response.status >= 500
        ? `Internal server error (HTTP ${response.status})`
        : `Operation failed (HTTP ${response.status})`;
    const detail =
      data &&
      typeof data === "object" &&
      "detail" in data &&
      typeof (data as { detail: unknown }).detail === "string"
        ? (data as { detail: string }).detail
        : fallback;
    throw new Error(detail);
  }

  return data as T;
}
