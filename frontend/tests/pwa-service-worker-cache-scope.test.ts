import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CLEAR_OFFLINE_FILE_CACHE_MESSAGE,
  OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE,
  OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
  OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
  OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE,
  offlineFileServiceWorkerCacheName,
} from "@/pwa/offlineFiles";

type CapturedRoute = {
  matcher: unknown;
  handler: unknown;
};

type CapturedStrategy = {
  cacheName: string;
  plugins: unknown[];
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

type CacheWriteGuard = (options: {
  response: Response;
}) => Promise<Response | null>;

const workbox = vi.hoisted(() => ({
  routes: [] as CapturedRoute[],
  strategies: [] as CapturedStrategy[],
  networkOnlyHandles: 0,
  nextCompletion: null as Promise<void> | null,
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
    constructor(options: { cacheName: string; plugins?: unknown[] }) {
      workbox.strategies.push({
        cacheName: options.cacheName,
        plugins: options.plugins ?? [],
      });
    }

    handleAll = vi.fn(() => {
      const completion = workbox.nextCompletion ?? Promise.resolve();
      workbox.nextCompletion = null;
      return [
        Promise.resolve(new Response(null, { status: 200 })),
        completion,
      ] as const;
    });
  },
  NetworkOnly: class NetworkOnly {
    handle = vi.fn(async () => {
      workbox.networkOnlyHandles += 1;
      return new Response(null, { status: 200 });
    });
  },
}));

let workerListeners: Map<string, Array<(event: unknown) => void>>;
let cacheNames: string[];
let deletedCacheNames: string[];
let windowClients: TestWindowClient[];
let clientsById: Map<string, TestWindowClient>;
let requestSequence: number;

function clientFor(id: string): TestWindowClient {
  const existing = clientsById.get(id);
  if (existing) return existing;
  const client = { id, postMessage: vi.fn() };
  clientsById.set(id, client);
  return client;
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

function contextMessage(
  context: { userId: number; vaultId: number },
  epoch = 0,
) {
  return {
    type: OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
    requestId: nextRequestId(),
    epoch,
    context,
  };
}

function clearMessage() {
  return {
    type: CLEAR_OFFLINE_FILE_CACHE_MESSAGE,
    requestId: nextRequestId(),
  };
}

async function activateWorker(): Promise<void> {
  const listener = workerListeners.get("activate")?.[0];
  if (!listener) throw new Error("service worker did not register an activate listener");
  let pending: Promise<unknown> | undefined;
  listener({
    waitUntil: (work: Promise<unknown>) => {
      pending = work;
    },
  });
  await pending;
}

function dispatchMessage(data: unknown, clientId = "client-1") {
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

async function sendMessage(data: unknown, clientId = "client-1"): Promise<TestWindowClient> {
  const dispatched = dispatchMessage(data, clientId);
  await dispatched.pending;
  return dispatched.source;
}

async function handleListing(clientId = "client-1"): Promise<Response> {
  return listingHandler()({
    event: { clientId },
    request: new Request("https://frostvault.test/api/files?page=1"),
  });
}

function cacheWriteGuard(): CacheWriteGuard {
  const strategy = workbox.strategies.at(-1);
  if (!strategy) throw new Error("service worker did not create a cache strategy");
  const plugin = strategy.plugins.find(
    (candidate): candidate is { cacheWillUpdate: CacheWriteGuard } =>
      Boolean(candidate) &&
      typeof candidate === "object" &&
      "cacheWillUpdate" in candidate &&
      typeof candidate.cacheWillUpdate === "function",
  );
  if (!plugin) throw new Error("service worker did not guard cache writes");
  return plugin.cacheWillUpdate;
}

function deferred<T>() {
  let resolve: (value: T | PromiseLike<T>) => void = () => undefined;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

describe("service-worker file-listing cache authorization scope", () => {
  beforeEach(async () => {
    vi.resetModules();
    workbox.routes.length = 0;
    workbox.strategies.length = 0;
    workbox.networkOnlyHandles = 0;
    workbox.nextCompletion = null;
    workerListeners = new Map();
    cacheNames = ["frostvault-file-listing"];
    deletedCacheNames = [];
    windowClients = [];
    clientsById = new Map();
    requestSequence = 0;

    vi.stubGlobal("caches", {
      keys: vi.fn(async () => [...cacheNames]),
      delete: vi.fn(async (cacheName: string) => {
        deletedCacheNames.push(cacheName);
        return true;
      }),
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

  it("deletes the legacy shared runtime cache on activation", async () => {
    await activateWorker();

    expect(deletedCacheNames).toEqual(["frostvault-file-listing"]);
  });

  it("does not use a URL-only cache before the client supplies an auth context", async () => {
    await handleListing("unidentified-client");

    expect(workbox.networkOnlyHandles).toBe(1);
    expect(workbox.strategies).toHaveLength(0);
  });

  it("uses separate cache namespaces when one User switches Vaults", async () => {
    const vaultA = { userId: 11, vaultId: 101 };
    const vaultB = { userId: 11, vaultId: 202 };
    const vaultACache = offlineFileServiceWorkerCacheName(vaultA);
    const vaultBCache = offlineFileServiceWorkerCacheName(vaultB);

    await sendMessage(contextMessage(vaultA));
    await handleListing();
    await sendMessage(contextMessage(vaultB));
    await handleListing();

    expect(workbox.strategies.map((strategy) => strategy.cacheName)).toEqual([
      vaultACache,
      vaultBCache,
    ]);
    expect(vaultBCache).not.toBe(vaultACache);
  });

  it("broadcasts an invalidation payload to every WindowClient and clears each context", async () => {
    const userAVault = { userId: 11, vaultId: 101 };
    const userBVault = { userId: 22, vaultId: 202 };
    const clientA = clientFor("client-a");
    const clientB = clientFor("client-b");
    windowClients = [clientA, clientB];

    await sendMessage(contextMessage(userAVault), "client-a");
    await sendMessage(contextMessage(userBVault), "client-b");
    await handleListing("client-b");
    expect(workbox.strategies.at(-1)?.cacheName).toBe(
      offlineFileServiceWorkerCacheName(userBVault),
    );

    const clear = clearMessage();
    await sendMessage(clear, "client-a");

    const invalidation = {
      type: OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE,
      epoch: 1,
    };
    expect(clientA.postMessage).toHaveBeenCalledWith(invalidation);
    expect(clientB.postMessage).toHaveBeenCalledWith(invalidation);
    expect(clientA.postMessage).toHaveBeenCalledWith({
      type: OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE,
      requestId: clear.requestId,
      epoch: 1,
    });

    await handleListing("client-b");
    expect(workbox.networkOnlyHandles).toBe(1);
  });

  it("rejects a delayed pre-clear context message until a fresh current-epoch context arrives", async () => {
    const userBVault = { userId: 22, vaultId: 202 };

    await sendMessage(contextMessage(userBVault), "client-b");
    await sendMessage(clearMessage(), "client-a");

    const stale = contextMessage(userBVault, 0);
    const staleClient = await sendMessage(stale, "client-b");
    expect(staleClient.postMessage).toHaveBeenLastCalledWith({
      type: OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
      requestId: stale.requestId,
      epoch: 1,
      accepted: false,
    });
    await handleListing("client-b");
    expect(workbox.networkOnlyHandles).toBe(1);

    const fresh = contextMessage(userBVault, 1);
    const freshClient = await sendMessage(fresh, "client-b");
    expect(freshClient.postMessage).toHaveBeenLastCalledWith({
      type: OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
      requestId: fresh.requestId,
      epoch: 1,
      accepted: true,
    });
    await handleListing("client-b");
    expect(workbox.strategies.at(-1)?.cacheName).toBe(
      offlineFileServiceWorkerCacheName(userBVault),
    );
  });

  it("waits for delayed old-epoch fetch work and refuses its late cache payload before acknowledging", async () => {
    const userAVault = { userId: 11, vaultId: 101 };
    await sendMessage(contextMessage(userAVault));

    const delayedCompletion = deferred<void>();
    const delayedPayload = deferred<Response>();
    const cacheWrites: Response[] = [];
    workbox.nextCompletion = delayedCompletion.promise;
    await handleListing();
    const guard = cacheWriteGuard();
    const oldFetch = delayedPayload.promise.then(async (response) => {
      const cacheable = await guard({ response });
      if (cacheable) cacheWrites.push(cacheable);
      delayedCompletion.resolve();
    });

    const clear = clearMessage();
    const dispatched = dispatchMessage(clear);
    await Promise.resolve();

    // The clear cannot purge or acknowledge while an old request is still
    // completing. Its delayed response reaches the epoch guard before the
    // simulated cache write, so it cannot recreate a cache after the purge.
    expect(deletedCacheNames).toEqual([]);
    expect(dispatched.source.postMessage).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE }),
    );

    delayedPayload.resolve(new Response("old", { status: 200 }));
    await oldFetch;
    await dispatched.pending;

    expect(cacheWrites).toEqual([]);
    expect(deletedCacheNames).toContain("frostvault-file-listing");
    expect(dispatched.source.postMessage).toHaveBeenCalledWith({
      type: OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE,
      requestId: clear.requestId,
      epoch: 1,
    });
  });
});
