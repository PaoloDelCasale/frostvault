import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
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
  type OfflineFileCacheGeneration,
} from "@/pwa/offlineFiles";

type CapturedRoute = {
  matcher: unknown;
  handler: unknown;
};

type CacheWriteGuard = (options: {
  response: Response;
}) => Promise<Response | null>;

type CapturedStrategy = {
  cacheName: string;
  cacheWriteGuard: CacheWriteGuard;
};

type TestWindowClient = {
  id: string;
  postMessage: ReturnType<typeof vi.fn>;
};

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

function deferred<T>() {
  let resolve: (value: T | PromiseLike<T>) => void = () => undefined;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

const workbox = vi.hoisted(() => ({
  routes: [] as CapturedRoute[],
  strategies: [] as CapturedStrategy[],
  networkOnlyHandles: 0,
  nextPayload: "authorized-payload",
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
    constructor(options: {
      cacheName: string;
      plugins?: Array<{ cacheWillUpdate?: CacheWriteGuard }>;
    }) {
      const cacheWriteGuard = options.plugins?.find(
        (plugin): plugin is { cacheWillUpdate: CacheWriteGuard } =>
          typeof plugin.cacheWillUpdate === "function",
      )?.cacheWillUpdate;
      if (!cacheWriteGuard) throw new Error("missing cache write guard");
      workbox.strategies.push({ cacheName: options.cacheName, cacheWriteGuard });
    }

    handleAll = vi.fn(({ request }: { request: Request }) => {
      const response = new Response(workbox.nextPayload, { status: 200 });
      const gate = workbox.nextCompletionGate ?? Promise.resolve();
      workbox.nextCompletionGate = null;
      const strategy = workbox.strategies.at(-1);
      if (!strategy) throw new Error("missing captured strategy");
      const completion = gate.then(async () => {
        const cacheable = await strategy.cacheWriteGuard({ response });
        if (!cacheable) return;
        const cache = await caches.open(strategy.cacheName);
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
  let pending: Promise<unknown> | undefined;
  const source = clientFor(clientId);
  messageListener()({
    data,
    source,
    waitUntil: (work) => {
      pending = work;
    },
  });
  return { pending, source };
}

async function activateWorker(): Promise<void> {
  const listener = workerListeners.get("activate")?.[0];
  if (!listener) throw new Error("service worker did not register an activate listener");
  const event = { waitUntil: vi.fn() };
  listener(event);
  await Promise.all(event.waitUntil.mock.calls.map(([work]) => work));
}

function requestGeneration(clientId = "client-a"): OfflineFileCacheGeneration {
  const requestId = nextRequestId();
  const { source } = dispatchMessage(
    { type: OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE, requestId },
    clientId,
  );
  const message = latestMessage(source, OFFLINE_FILE_CACHE_GENERATION_MESSAGE);
  expect(message.requestId).toBe(requestId);
  return message.generation as OfflineFileCacheGeneration;
}

function registerContext(
  clientId: string,
  context: { userId: number; vaultId: number },
  generation: OfflineFileCacheGeneration,
  transitionId?: string,
): Record<string, unknown> {
  const requestId = nextRequestId();
  const { source } = dispatchMessage(
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
    source,
    OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
  );
  expect(acknowledgement.requestId).toBe(requestId);
  return acknowledgement;
}

function beginTransition(clientId = "client-a", transitionId = "transition-a") {
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

async function handleListing(
  clientId: string,
  generation: OfflineFileCacheGeneration,
): Promise<Response> {
  return listingHandler()({
    event: { clientId },
    request: new Request("https://frostvault.test/api/files?page=1", {
      headers: {
        [OFFLINE_FILE_CACHE_GENERATION_HEADER]: offlineFileCacheGenerationKey(
          generation,
        ),
      },
    }),
  });
}

describe("service-worker file-listing cache authorization scope", () => {
  beforeEach(async () => {
    vi.resetModules();
    workbox.routes.length = 0;
    workbox.strategies.length = 0;
    workbox.networkOnlyHandles = 0;
    workbox.nextPayload = "authorized-payload";
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

  it("uses a generation-scoped CacheStorage payload for an unchanged authorization context", async () => {
    const client = clientFor("client-a");
    windowClients = [client];
    await activateWorker();
    const generation = requestGeneration();
    const context = { userId: 11, vaultId: 101 };

    expect(registerContext("client-a", context, generation)).toMatchObject({
      accepted: true,
      closed: false,
      transitionComplete: false,
    });
    workbox.nextPayload = "first-authorized-listing";
    const response = await handleListing("client-a", generation);
    expect(await response.text()).toBe("first-authorized-listing");
    await Promise.resolve();
    await Promise.resolve();

    const cacheName = offlineFileServiceWorkerCacheName(context, generation);
    const cached = await memoryCache(cacheName).match(
      new Request("https://frostvault.test/api/files?page=1"),
    );
    expect(await cached?.text()).toBe("first-authorized-listing");

    // A normal refresh of unchanged User/Vault authorization remains available
    // and uses the same current-generation namespace rather than clearing it.
    expect(registerContext("client-a", context, generation)).toMatchObject({
      accepted: true,
      closed: false,
    });
    expect(workbox.strategies.map((strategy) => strategy.cacheName)).toEqual([
      cacheName,
    ]);
  });

  it("keeps other WindowClients closed and rejects old-context registration until the mutating client completes", async () => {
    const clientA = clientFor("client-a");
    const clientB = clientFor("client-b");
    windowClients = [clientA, clientB];
    await activateWorker();
    const beforeMutation = requestGeneration("client-a");
    const oldContext = { userId: 11, vaultId: 101 };
    const otherContext = { userId: 22, vaultId: 202 };

    expect(registerContext("client-a", oldContext, beforeMutation)).toMatchObject({
      accepted: true,
    });
    expect(registerContext("client-b", otherContext, beforeMutation)).toMatchObject({
      accepted: true,
    });

    const started = beginTransition("client-a", "vault-change-a");
    expect(started.acknowledgement).toMatchObject({
      accepted: true,
      closed: true,
      transitionComplete: false,
    });
    const closedGeneration = started.acknowledgement
      .generation as OfflineFileCacheGeneration;
    expect(closedGeneration).not.toEqual(beforeMutation);
    await Promise.resolve();
    await Promise.resolve();
    expect(latestMessage(clientB, OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE)).toMatchObject({
      generation: closedGeneration,
      closed: true,
    });

    // Neither a delayed pre-mutation response nor a fresh probe by another
    // tab owns the opaque transition capability, so both registrations fail.
    expect(registerContext("client-b", otherContext, beforeMutation)).toMatchObject({
      accepted: false,
      closed: true,
    });
    expect(registerContext("client-b", otherContext, closedGeneration)).toMatchObject({
      accepted: false,
      closed: true,
    });
    const noLeaseResponse = await handleListing("client-b", closedGeneration);
    expect(await noLeaseResponse.text()).toBe("network-only");
    expect(workbox.networkOnlyHandles).toBe(1);

    const completed = registerContext(
      "client-a",
      { userId: 11, vaultId: 202 },
      closedGeneration,
      "vault-change-a",
    );
    expect(completed).toMatchObject({
      accepted: true,
      closed: false,
      transitionComplete: true,
    });
    const reopenedGeneration = completed.generation as OfflineFileCacheGeneration;
    expect(reopenedGeneration).not.toEqual(closedGeneration);
    await Promise.resolve();
    await Promise.resolve();
    expect(latestMessage(clientB, OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE)).toMatchObject({
      generation: reopenedGeneration,
      closed: false,
    });
  });

  it("does not wait for a hung old fetch and prevents its actual CacheStorage put after begin", async () => {
    const client = clientFor("client-a");
    windowClients = [client];
    await activateWorker();
    const generation = requestGeneration();
    const context = { userId: 11, vaultId: 101 };
    registerContext("client-a", context, generation);

    const delayedCompletion = deferred<void>();
    workbox.nextPayload = "late-old-payload";
    workbox.nextCompletionGate = delayedCompletion.promise;
    await handleListing("client-a", generation);
    const cacheName = offlineFileServiceWorkerCacheName(context, generation);
    const request = new Request("https://frostvault.test/api/files?page=1");

    const started = beginTransition("client-a", "hung-fetch-transition");
    // The ACK is synchronous state closure; it is not held behind Workbox's
    // completion promise, so the server mutation can start immediately.
    expect(started.acknowledgement).toMatchObject({ accepted: true, closed: true });
    expect(await memoryCache(cacheName).match(request)).toBeUndefined();

    delayedCompletion.resolve();
    await Promise.resolve();
    await Promise.resolve();
    // cacheWillUpdate runs directly before put and sees the closed/new
    // generation, so the real fake CacheStorage never receives stale bytes.
    expect(await memoryCache(cacheName).match(request)).toBeUndefined();
  });
});
