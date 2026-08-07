/** Catalog revision invalidation transport for event-driven archive updates. */

export type CatalogInvalidationDomains =
  | "files"
  | "stats"
  | "rename_candidates"
  | (string & {});

export type CatalogRevisionSignal = {
  vault_id: number;
  revision: number;
  domains: CatalogInvalidationDomains[];
  has_gap: boolean;
  changed?: boolean;
};

export type CatalogHelloSignal = {
  vault_id: number;
  revision: number;
};

const DEFAULT_DOMAINS: CatalogInvalidationDomains[] = [
  "files",
  "stats",
  "rename_candidates",
];

export function catalogEventsUrl(afterRevision = 0): string {
  const params = new URLSearchParams();
  if (afterRevision > 0) {
    params.set("after_revision", String(afterRevision));
  }
  const query = params.toString();
  return query ? `/api/catalog/events?${query}` : "/api/catalog/events";
}

export function catalogRevisionUrl(afterRevision = 0): string {
  const params = new URLSearchParams();
  if (afterRevision > 0) {
    params.set("after_revision", String(afterRevision));
  }
  const query = params.toString();
  return query
    ? `/api/catalog/revision?${query}`
    : "/api/catalog/revision";
}

export async function fetchCatalogRevision(
  afterRevision = 0,
  init?: RequestInit,
): Promise<CatalogRevisionSignal> {
  const response = await fetch(catalogRevisionUrl(afterRevision), {
    credentials: "same-origin",
    ...init,
  });
  if (!response.ok) {
    throw new Error(`catalog revision fetch failed: ${response.status}`);
  }
  const body = (await response.json()) as CatalogRevisionSignal;
  return {
    vault_id: Number(body.vault_id),
    revision: Number(body.revision) || 0,
    domains: normalizeDomains(body.domains),
    has_gap: Boolean(body.has_gap),
    changed: Boolean(body.changed),
  };
}

export function normalizeDomains(
  value: unknown,
): CatalogInvalidationDomains[] {
  if (!Array.isArray(value) || value.length === 0) {
    return [...DEFAULT_DOMAINS];
  }
  const seen = new Set<string>();
  const domains: CatalogInvalidationDomains[] = [];
  for (const item of value) {
    const domain = String(item ?? "").trim();
    if (!domain || seen.has(domain)) continue;
    seen.add(domain);
    domains.push(domain);
  }
  return domains.length > 0 ? domains : [...DEFAULT_DOMAINS];
}

export function parseCatalogEventData(
  raw: string,
): CatalogRevisionSignal | null {
  try {
    const body = JSON.parse(raw) as Partial<CatalogRevisionSignal>;
    if (body.vault_id == null || body.revision == null) return null;
    return {
      vault_id: Number(body.vault_id),
      revision: Number(body.revision) || 0,
      domains: normalizeDomains(body.domains),
      has_gap: Boolean(body.has_gap),
    };
  } catch {
    return null;
  }
}

export type CatalogEventHandlers = {
  onHello?: (signal: CatalogHelloSignal) => void;
  onCatalog?: (signal: CatalogRevisionSignal) => void;
  onError?: (error: { error: string; vault_id?: number }) => void;
  onConnectionError?: () => void;
};

export type CatalogEventSource = {
  close: () => void;
};

type EventSourceLike = {
  close: () => void;
  addEventListener: (
    type: string,
    listener: (event: MessageEvent<string>) => void,
  ) => void;
  onerror: ((ev: Event) => unknown) | null;
};

export type CatalogEventSourceFactory = (
  url: string,
) => EventSourceLike;

/** Resolve EventSource without assuming a browser global exists (jsdom/tests). */
export function getDefaultEventSourceFactory(): CatalogEventSourceFactory {
  return (url: string): EventSourceLike => {
    const globalObj = globalThis as typeof globalThis & {
      EventSource?: new (
        url: string | URL,
        eventSourceInitDict?: EventSourceInit,
      ) => EventSourceLike;
    };
    const Ctor = globalObj.EventSource;
    if (typeof Ctor === "function") {
      return new Ctor(url, { withCredentials: true }) as EventSourceLike;
    }
    // Fail closed without throwing: unit tests that mount App without a browser
    // EventSource still exercise auth/offline seams. Production browsers always
    // provide EventSource; inject createSource when a real stream is required.
    return {
      close: () => undefined,
      addEventListener: () => undefined,
      onerror: null,
    };
  };
}

const defaultEventSourceFactory: CatalogEventSourceFactory =
  getDefaultEventSourceFactory();

/**
 * Open an authenticated SSE subscription for catalog invalidation signals.
 * Callers own reconnect/backoff policy; this helper only binds one connection.
 * Pass ``createSource`` in tests; production uses the guarded EventSource global.
 */
export function openCatalogEventSource(
  afterRevision: number,
  handlers: CatalogEventHandlers,
  createSource: CatalogEventSourceFactory = defaultEventSourceFactory,
): CatalogEventSource {
  const source = createSource(catalogEventsUrl(afterRevision));
  let closed = false;

  source.addEventListener("hello", (event) => {
    if (closed) return;
    try {
      const body = JSON.parse(event.data) as CatalogHelloSignal;
      handlers.onHello?.({
        vault_id: Number(body.vault_id),
        revision: Number(body.revision) || 0,
      });
    } catch {
      // Ignore malformed hello frames; reconnect policy handles stalls.
    }
  });

  source.addEventListener("catalog", (event) => {
    if (closed) return;
    const signal = parseCatalogEventData(event.data);
    if (signal) handlers.onCatalog?.(signal);
  });

  source.addEventListener("error", (event) => {
    if (closed) return;
    // Named SSE "error" events carry a JSON body; transport errors use onerror.
    if (typeof event.data === "string" && event.data) {
      try {
        const body = JSON.parse(event.data) as {
          error?: string;
          vault_id?: number;
        };
        if (body.error) {
          handlers.onError?.({
            error: body.error,
            vault_id:
              body.vault_id == null ? undefined : Number(body.vault_id),
          });
          return;
        }
      } catch {
        // fall through to connection error
      }
    }
  });

  source.onerror = () => {
    if (closed) return;
    handlers.onConnectionError?.();
  };

  return {
    close: () => {
      closed = true;
      source.close();
    },
  };
}
