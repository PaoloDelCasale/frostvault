import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER,
  OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
  OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
  OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
  OFFLINE_FILE_CACHE_GENERATION_HEADER,
  OFFLINE_FILE_CACHE_GENERATION_MESSAGE,
  OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE,
  OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE,
  OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE,
  offlineFileCacheGenerationKey,
  offlineFileServiceWorkerCacheName,
  type OfflineCacheContext,
  type OfflineFileCacheGeneration,
} from "@/pwa/offlineFiles";

type CapturedRoute = { matcher: unknown; handler: unknown };
type CacheWriteGuard = (options: { response: Response }) => Promise<Response | null>;
type CapturedStrategy = { cacheName: string; cacheWriteGuard: CacheWriteGuard };
type TestWindowClient = { id: string; postMessage: ReturnType<typeof vi.fn> };
type WorkerMessageListener = (event: {
  data: unknown;
  source: TestWindowClient;
  waitUntil: (work: Promise<unknown>) => void;
}) => void;
type ListingHandler = (options: {
  event: { clientId: string };
  request: Request;
}) => Promise<Response>;
type MemoryCache = {
  put: (request: Request, response: Response) => Promise<void>;
  match: (request: Request) => Promise<Response | undefined>;
};

const userA: OfflineCacheContext = {
  userId: 11,
  vaultId: 101,
  authorizationGeneration: "server-session-a-vault-a",
};
const userB: OfflineCacheContext = {
  userId: 22,
  vaultId: 202,
  authorizationGeneration: "server-session-b-vault-b",
};

const workbox = vi.hoisted(() => ({
  routes: [] as CapturedRoute[],
  strategies: [] as CapturedStrategy[],
  networkOnlyHandles: 0,
  nextPayload: "authorized-payload",
  nextAuthorization: null as string | null,
  nextCompletionGate: null as Promise<void> | null,
}));

vi.mock("workbox-core", () => ({ clientsClaim: vi.fn() }));
vi.mock("workbox-expiration", () => ({
  ExpirationPlugin: class ExpirationPlugin {},
}));
vi.mock("workbox-precaching", () => ({
  cleanupOutdatedCaches: vi.fn(),
  precacheAndRoute: vi.fn(),
}));
vi.mock("workbox-routing", () => ({
  registerRoute: vi.fn((matcher: unknown, handler: unknown) => {
    workbox.routes.push({ matcher, handler });
  }),
}));
vi.mock("workbox-strategies", () => ({
  NetworkFirst: class NetworkFirst {
    private readonly captured: CapturedStrategy;

    constructor(options: {
      cacheName: string;
      plugins?: Array<{ cacheWillUpdate?: CacheWriteGuard }>;
    }) {
      const cacheWriteGuard = options.plugins?.find(
        (plugin): plugin is { cacheWillUpdate: CacheWriteGuard } =>
          typeof plugin.cacheWillUpdate === "function",
      )?.cacheWillUpdate;
      if (!cacheWriteGuard) throw new Error("missing cache write guard");
      this.captured = { cacheName: options.cacheName, cacheWriteGuard };
      workbox.strategies.push(this.captured);
    }

    handleAll = vi.fn(({ request }: { request: Request }) => {
      const authorization =
        workbox.nextAuthorization ??
        request.headers.get(OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER) ??
        "";
      const response = new Response(workbox.nextPayload, {
        status: 200,
        headers: { [OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER]: authorization },
      });
      const gate = workbox.nextCompletionGate ?? Promise.resolve();
      workbox.nextCompletionGate = null;
      workbox.nextAuthorization = null;
      const completion = gate.then(async () => {
        const cacheable = await this.captured.cacheWriteGuard({ response });
        if (!cacheable) return;
        const cache = await caches.open(this.captured.cacheName);
        await cache.put(request, cacheable.clone());
      });
      return [Promise.resolve(response), completion] as const;
    });
  },
  NetworkOnly: class NetworkOnly {
    handle = vi.fn(async () => {
      workbox.networkOnlyHandles += 1;
      return new Response("network-only", { status: 200 });
    });
  },
}));

let workerListeners: Map<string, Array<(event: unknown) => void>>;
let windowClients: TestWindowClient[];
let clientsById: Map<string, TestWindowClient>;
let memoryCaches: Map<string, Map<string, Response>>;
let requestSequence: number;

function clientFor(id: string): TestWindowClient {
  const existing = clientsById.get(id);
  if (existing) return existing;
  const client = { id, postMessage: vi.fn() };
  clientsById.set(id, client);
  return client;
}

function memoryCache(name: string): MemoryCache {
  const entries = memoryCaches.get(name) ?? new Map<string, Response>();
  memoryCaches.set(name, entries);
  return {
    async put(request, response) {
      entries.set(request.url, response.clone());
    },
    async match(request) {
      const response = entries.get(request.url);
      return response?.clone();
    },
  };
}

function messageListener(): WorkerMessageListener {
  const listener = workerListeners.get("message")?.[0];
  if (!listener) throw new Error("service worker did not register a message listener");
  return listener as WorkerMessageListener;
}

function listingHandler(): ListingHandler {
  const route = workbox.routes[0];
  if (!route) throw new Error("service worker did not register the file-list route");
  return route.handler as ListingHandler;
}

function nextRequestId(): string {
  requestSequence += 1;
  return `request-${requestSequence}`;
}

function messagesOfType(
  client: TestWindowClient,
  type: string,
): Array<Record<string, unknown>> {
  return client.postMessage.mock.calls
    .map(([message]) => message)
    .filter(
      (message): message is Record<string, unknown> =>
        Boolean(message) &&
        typeof message === "object" &&
        (message as { type?: unknown }).type === type,
    );
}

function latestMessage(
  client: TestWindowClient,
  type: string,
): Record<string, unknown> {
  const message = messagesOfType(client, type).at(-1);
  if (!message) throw new Error(`client ${client.id} did not receive ${type}`);
  return message;
}

function dispatchMessage(data: unknown, clientId = "client-a") {
  const pending: Promise<unknown>[] = [];
  const source = clientFor(clientId);
  messageListener()({
    data,
    source,
    waitUntil: (work) => pending.push(work),
  });
  return { pending, source };
}

async function settle(dispatched: { pending: Promise<unknown>[] }): Promise<void> {
  await Promise.all(dispatched.pending);
}

async function activateWorker(): Promise<void> {
  const listener = workerListeners.get("activate")?.[0];
  if (!listener) throw new Error("service worker did not register an activate listener");
  const pending: Promise<unknown>[] = [];
  listener({ waitUntil: (work: Promise<unknown>) => pending.push(work) });
  await Promise.all(pending);
}

function requestGeneration(clientId: string): OfflineFileCacheGeneration {
  const requestId = nextRequestId();
  const { source } = dispatchMessage(
    { type: OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE, requestId },
    clientId,
  );
  const message = latestMessage(source, OFFLINE_FILE_CACHE_GENERATION_MESSAGE);
  expect(message.requestId).toBe(requestId);
  return message.generation as OfflineFileCacheGeneration;
}

function beginTransition(clientId: string, transitionId: string) {
  const requestId = nextRequestId();
  const dispatched = dispatchMessage(
    {
      type: OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
      requestId,
      transitionId,
    },
    clientId,
  );
  const acknowledgement = latestMessage(
    dispatched.source,
    OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE,
  );
  expect(acknowledgement.requestId).toBe(requestId);
  return { ...dispatched, acknowledgement };
}

function registerContext(
  clientId: string,
  context: OfflineCacheContext,
  generation: OfflineFileCacheGeneration,
  transitionId?: string,
) {
  const requestId = nextRequestId();
  const dispatched = dispatchMessage(
    {
      type: OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
      requestId,
      generation,
      context,
      ...(transitionId ? { transitionId } : {}),
    },
    clientId,
  );
  const acknowledgement = latestMessage(
    dispatched.source,
    OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
  );
  expect(acknowledgement.requestId).toBe(requestId);
  return { ...dispatched, acknowledgement };
}

async function handleListing(
  clientId: string,
  context: OfflineCacheContext,
  generation: OfflineFileCacheGeneration,
): Promise<Response> {
  return listingHandler()({
    event: { clientId },
    request: new Request("https://frostvault.test/api/files?page=1", {
      headers: {
        [OFFLINE_FILE_CACHE_GENERATION_HEADER]: offlineFileCacheGenerationKey(
          generation,
        ),
        [OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER]: context.authorizationGeneration,
      },
    }),
  });
}

async function flushWorkbox(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

describe("service-worker file-listing cache authorization scope", () => {
  beforeEach(async () => {
    vi.resetModules();
    workbox.routes.length = 0;
    workbox.strategies.length = 0;
    workbox.networkOnlyHandles = 0;
    workbox.nextPayload = "authorized-payload";
    workbox.nextAuthorization = null;
    workbox.nextCompletionGate = null;
    workerListeners = new Map();
    windowClients = [];
    clientsById = new Map();
    memoryCaches = new Map();
    requestSequence = 0;

    vi.stubGlobal("caches", {
      keys: vi.fn(async () => [...memoryCaches.keys()]),
      delete: vi.fn(async (name: string) => memoryCaches.delete(name)),
      open: vi.fn(async (name: string) => memoryCache(name)),
    });
    vi.stubGlobal("self", {
      __WB_MANIFEST: [],
      skipWaiting: vi.fn(async () => undefined),
      addEventListener: (type: string, listener: (event: unknown) => void) => {
        const listeners = workerListeners.get(type) ?? [];
        listeners.push(listener);
        workerListeners.set(type, listeners);
      },
      registration: { showNotification: vi.fn() },
      clients: {
        matchAll: vi.fn(async () => windowClients),
        openWindow: vi.fn(async () => undefined),
      },
    });

    await import("../src/sw");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("starts closed after boot, broadcasts the first boot nonce, and rejects normal registration", async () => {
    const clientA = clientFor("client-a");
    const clientB = clientFor("client-b");
    windowClients = [clientA, clientB];
    await activateWorker();

    const generation = requestGeneration("client-a");
    expect(latestMessage(clientA, OFFLINE_FILE_CACHE_GENERATION_MESSAGE)).toMatchObject({
      generation,
      closed: true,
    });
    await flushWorkbox();
    expect(latestMessage(clientB, OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE)).toMatchObject({
      generation,
      closed: true,
    });

    const rejected = registerContext("client-a", userA, generation);
    expect(rejected.acknowledgement).toMatchObject({
      accepted: false,
      closed: true,
    });
  });

  it("keeps the same payload/cache available for an unchanged authorization navigation", async () => {
    const clientA = clientFor("client-a");
    windowClients = [clientA];
    await activateWorker();
    requestGeneration("client-a");
    const started = beginTransition("client-a", "login-a");
    const closedGeneration = started.acknowledgement
      .generation as OfflineFileCacheGeneration;
    const completed = registerContext("client-a", userA, closedGeneration, "login-a");
    const openGeneration = completed.acknowledgement
      .generation as OfflineFileCacheGeneration;
    expect(completed.acknowledgement).toMatchObject({
      accepted: true,
      closed: false,
      transitionComplete: true,
    });

    workbox.nextPayload = "user-a-payload";
    const first = await handleListing("client-a", userA, openGeneration);
    expect(await first.text()).toBe("user-a-payload");
    await flushWorkbox();
    const cacheName = offlineFileServiceWorkerCacheName(userA, openGeneration);
    const request = new Request("https://frostvault.test/api/files?page=1");
    expect(await (await memoryCache(cacheName).match(request))?.text()).toBe(
      "user-a-payload",
    );

    const unchanged = registerContext("client-a", userA, openGeneration);
    expect(unchanged.acknowledgement).toMatchObject({
      accepted: true,
      closed: false,
      transitionComplete: false,
    });
    expect(workbox.strategies.map((strategy) => strategy.cacheName)).toEqual([
      cacheName,
    ]);
  });

  it("uses real two-client payloads across logout then a different user without serving User A", async () => {
    const clientA = clientFor("client-a");
    const clientB = clientFor("client-b");
    windowClients = [clientA, clientB];
    await activateWorker();
    requestGeneration("client-a");

    const loginA = beginTransition("client-a", "login-a");
    const aClosed = loginA.acknowledgement.generation as OfflineFileCacheGeneration;
    const aCompleted = registerContext("client-a", userA, aClosed, "login-a");
    const aOpen = aCompleted.acknowledgement.generation as OfflineFileCacheGeneration;
    workbox.nextPayload = "user-a-secret-listing";
    expect(await (await handleListing("client-a", userA, aOpen)).text()).toBe(
      "user-a-secret-listing",
    );
    await flushWorkbox();
    const cacheA = offlineFileServiceWorkerCacheName(userA, aOpen);
    const request = new Request("https://frostvault.test/api/files?page=1");
    expect(await (await memoryCache(cacheA).match(request))?.text()).toBe(
      "user-a-secret-listing",
    );

    const logout = beginTransition("client-a", "logout-a");
    const logoutGeneration = logout.acknowledgement
      .generation as OfflineFileCacheGeneration;
    await settle(logout);
    expect(latestMessage(clientB, OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE)).toMatchObject({
      generation: logoutGeneration,
      closed: true,
    });
    expect(await (await handleListing("client-a", userA, aOpen)).text()).toBe(
      "network-only",
    );

    // A new sign-in intentionally supersedes the interrupted logout capability.
    const loginB = beginTransition("client-b", "login-b");
    const bClosed = loginB.acknowledgement.generation as OfflineFileCacheGeneration;
    const bCompleted = registerContext("client-b", userB, bClosed, "login-b");
    const bOpen = bCompleted.acknowledgement.generation as OfflineFileCacheGeneration;
    workbox.nextPayload = "user-b-private-listing";
    expect(await (await handleListing("client-b", userB, bOpen)).text()).toBe(
      "user-b-private-listing",
    );
    await flushWorkbox();

    expect(await (await handleListing("client-a", userA, aOpen)).text()).toBe(
      "network-only",
    );
    const cacheB = offlineFileServiceWorkerCacheName(userB, bOpen);
    expect(await (await memoryCache(cacheB).match(request))?.text()).toBe(
      "user-b-private-listing",
    );
    expect(await memoryCache(cacheA).match(request)).toBeUndefined();
  });

  it("does not let a lost transition capability reopen the worker", async () => {
    const clientA = clientFor("client-a");
    const clientB = clientFor("client-b");
    windowClients = [clientA, clientB];
    await activateWorker();
    requestGeneration("client-a");

    const old = beginTransition("client-a", "lost-a");
    const oldGeneration = old.acknowledgement.generation as OfflineFileCacheGeneration;
    const replacement = beginTransition("client-b", "replacement-b");
    const replacementGeneration = replacement.acknowledgement
      .generation as OfflineFileCacheGeneration;
    expect(replacementGeneration).not.toEqual(oldGeneration);

    const rejected = registerContext("client-a", userA, replacementGeneration, "lost-a");
    expect(rejected.acknowledgement).toMatchObject({
      accepted: false,
      closed: true,
    });
    const accepted = registerContext(
      "client-b",
      userB,
      replacementGeneration,
      "replacement-b",
    );
    expect(accepted.acknowledgement).toMatchObject({
      accepted: true,
      closed: false,
      transitionComplete: true,
    });
  });

  it("refuses a server payload whose authorization header no longer matches the lease", async () => {
    const clientA = clientFor("client-a");
    windowClients = [clientA];
    await activateWorker();
    requestGeneration("client-a");
    const started = beginTransition("client-a", "login-a");
    const closed = started.acknowledgement.generation as OfflineFileCacheGeneration;
    const completed = registerContext("client-a", userA, closed, "login-a");
    const open = completed.acknowledgement.generation as OfflineFileCacheGeneration;

    workbox.nextPayload = "stale-server-payload";
    workbox.nextAuthorization = "server-session-after-switch";
    expect(await (await handleListing("client-a", userA, open)).text()).toBe(
      "stale-server-payload",
    );
    await flushWorkbox();
    const cache = memoryCache(offlineFileServiceWorkerCacheName(userA, open));
    expect(
      await cache.match(new Request("https://frostvault.test/api/files?page=1")),
    ).toBeUndefined();
  });
});
