import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CLEAR_OFFLINE_FILE_CACHE_MESSAGE,
  OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
  offlineFileServiceWorkerCacheName,
} from "@/pwa/offlineFiles";

type CapturedRoute = {
  matcher: unknown;
  handler: unknown;
};

type CapturedStrategy = {
  cacheName: string;
};

type WorkerMessageListener = (event: {
  data: unknown;
  source: { id: string };
  waitUntil: (work: Promise<unknown>) => void;
}) => void;

type ListingHandler = (options: {
  event: { clientId: string };
  request: Request;
}) => Promise<unknown>;

const workbox = vi.hoisted(() => ({
  routes: [] as CapturedRoute[],
  strategies: [] as CapturedStrategy[],
  networkOnlyHandles: 0,
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
    constructor(options: { cacheName: string }) {
      workbox.strategies.push({ cacheName: options.cacheName });
    }

    handle = vi.fn(async () => new Response(null, { status: 200 }));
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

async function sendMessage(data: unknown, clientId = "client-1"): Promise<void> {
  let pending: Promise<unknown> | undefined;
  messageListener()({
    data,
    source: { id: clientId },
    waitUntil: (work) => {
      pending = work;
    },
  });
  await pending;
}

async function handleListing(clientId = "client-1"): Promise<void> {
  await listingHandler()({
    event: { clientId },
    request: new Request("https://frostvault.test/api/files?page=1"),
  });
}

describe("service-worker file-listing cache authorization scope", () => {
  beforeEach(async () => {
    vi.resetModules();
    workbox.routes.length = 0;
    workbox.strategies.length = 0;
    workbox.networkOnlyHandles = 0;
    workerListeners = new Map();
    cacheNames = ["frostvault-file-listing"];
    deletedCacheNames = [];


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
        matchAll: vi.fn(async () => []),
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

  it("purges User A on logout and creates a distinct cache for User B", async () => {
    const userAVault = { userId: 11, vaultId: 101 };
    const userBVault = { userId: 22, vaultId: 101 };
    const userACache = offlineFileServiceWorkerCacheName(userAVault);
    const userBCache = offlineFileServiceWorkerCacheName(userBVault);
    cacheNames.push(userACache, userBCache);

    await sendMessage({
      type: OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
      context: userAVault,
    });
    await handleListing();
    expect(workbox.strategies.at(-1)?.cacheName).toBe(userACache);

    await sendMessage({ type: CLEAR_OFFLINE_FILE_CACHE_MESSAGE });
    expect(deletedCacheNames).toEqual(
      expect.arrayContaining(["frostvault-file-listing", userACache, userBCache]),
    );

    await sendMessage({
      type: OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
      context: userBVault,
    });
    await handleListing();
    expect(workbox.strategies.at(-1)?.cacheName).toBe(userBCache);
    expect(userBCache).not.toBe(userACache);
  });

  it("makes every controlled client network-only after another client clears until fresh auth", async () => {
    const userAVault = { userId: 11, vaultId: 101 };
    const userBVault = { userId: 22, vaultId: 202 };
    const userBCache = offlineFileServiceWorkerCacheName(userBVault);

    await sendMessage(
      { type: OFFLINE_FILE_CACHE_CONTEXT_MESSAGE, context: userAVault },
      "client-a",
    );
    await sendMessage(
      { type: OFFLINE_FILE_CACHE_CONTEXT_MESSAGE, context: userBVault },
      "client-b",
    );
    await handleListing("client-b");
    expect(workbox.strategies.at(-1)?.cacheName).toBe(userBCache);

    // User A's logout/transition must invalidate Client B as well.
    await sendMessage({ type: CLEAR_OFFLINE_FILE_CACHE_MESSAGE }, "client-a");
    await handleListing("client-b");
    expect(workbox.networkOnlyHandles).toBe(1);
    expect(workbox.strategies).toHaveLength(1);

    // Only a fresh /api/me-driven context message may re-enable caching for B.
    await sendMessage(
      { type: OFFLINE_FILE_CACHE_CONTEXT_MESSAGE, context: userBVault },
      "client-b",
    );
    await handleListing("client-b");
    expect(workbox.strategies.at(-1)?.cacheName).toBe(userBCache);
    expect(workbox.strategies).toHaveLength(2);
  });

  it("uses separate cache namespaces when one User switches Vaults", async () => {
    const vaultA = { userId: 11, vaultId: 101 };
    const vaultB = { userId: 11, vaultId: 202 };
    const vaultACache = offlineFileServiceWorkerCacheName(vaultA);
    const vaultBCache = offlineFileServiceWorkerCacheName(vaultB);

    await sendMessage({
      type: OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
      context: vaultA,
    });
    await handleListing();
    await sendMessage({
      type: OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
      context: vaultB,
    });
    await handleListing();

    expect(workbox.strategies.map((strategy) => strategy.cacheName)).toEqual([
      vaultACache,
      vaultBCache,
    ]);
    expect(vaultBCache).not.toBe(vaultACache);
  });
});
