import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FilesResponse } from "@/api/types";

type OfflineFilesModule = typeof import("@/pwa/offlineFiles");
type ServiceWorkerMessageListener = (event: MessageEvent<unknown>) => void;
type ControllerChangeListener = () => void;

type WorkerGeneration = { bootId: string; counter: number };

const contextA = {
  userId: 11,
  vaultId: 101,
  authorizationGeneration: "server-session-a-vault-a",
};
const contextB = {
  userId: 22,
  vaultId: 202,
  authorizationGeneration: "server-session-b-vault-b",
};

function listing(name: string): FilesResponse {
  return {
    items: [
      {
        type: "file",
        name,
        path: name,
        state: "both",
        local_size: 12,
      },
    ],
    total: 1,
    page: 1,
    directory: "",
    mode: "browse",
  };
}

describe("offline file-cache client transition protocol", () => {
  let offlineFiles: OfflineFilesModule;
  let postedMessages: Array<Record<string, unknown>>;
  let messageListeners: Set<ServiceWorkerMessageListener>;
  let controllerChangeListeners: Set<ControllerChangeListener>;

  beforeEach(async () => {
    vi.resetModules();
    localStorage.clear();
    postedMessages = [];
    messageListeners = new Set();
    controllerChangeListeners = new Set();
    const worker = {
      postMessage: vi.fn((message: Record<string, unknown>) => {
        postedMessages.push(message);
      }),
    };
    vi.stubGlobal("navigator", {
      ...navigator,
      serviceWorker: {
        controller: worker,
        addEventListener: (type: string, listener: ServiceWorkerMessageListener) => {
          if (type === "message") messageListeners.add(listener);
          if (type === "controllerchange") {
            controllerChangeListeners.add(listener as unknown as ControllerChangeListener);
          }
        },
        removeEventListener: (
          type: string,
          listener: ServiceWorkerMessageListener,
        ) => {
          if (type === "message") messageListeners.delete(listener);
          if (type === "controllerchange") {
            controllerChangeListeners.delete(listener as unknown as ControllerChangeListener);
          }
        },
        getRegistration: vi.fn(async () => undefined),
      },
    });
    offlineFiles = await import("@/pwa/offlineFiles");
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  function deliver(data: unknown): void {
    const event = new MessageEvent("message", { data });
    for (const listener of messageListeners) listener(event);
  }

  function controllerChange(): void {
    for (const listener of controllerChangeListeners) listener();
  }

  async function nextPostedMessage(): Promise<Record<string, unknown>> {
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const message = postedMessages.shift();
      if (message) return message;
      await Promise.resolve();
    }
    throw new Error("client did not post a Service Worker message");
  }

  async function prepare(
    generation: WorkerGeneration,
    closed = false,
  ): Promise<Awaited<ReturnType<OfflineFilesModule["prepareOfflineFileCacheContext"]>>> {
    const freshnessPromise = offlineFiles.prepareOfflineFileCacheContext();
    const request = await nextPostedMessage();
    expect(request.type).toBe(
      offlineFiles.OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE,
    );
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_GENERATION_MESSAGE,
      requestId: request.requestId,
      generation,
      closed,
    });
    return freshnessPromise;
  }

  async function grantInitialLease(generation: WorkerGeneration) {
    const freshness = await prepare(generation);
    const leasePromise = offlineFiles.setOfflineFileCacheContext(contextA, freshness);
    const contextMessage = await nextPostedMessage();
    expect(contextMessage.type).toBe(offlineFiles.OFFLINE_FILE_CACHE_CONTEXT_MESSAGE);
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
      requestId: contextMessage.requestId,
      generation,
      accepted: true,
      closed: false,
      transitionComplete: false,
    });
    const lease = await leasePromise;
    expect(lease).not.toBeNull();
    return lease!;
  }

  it("keeps a payload available across unchanged navigation only with the same server generation", async () => {
    const generation = { bootId: "worker-a", counter: 1 };
    const firstLease = await grantInitialLease(generation);
    offlineFiles.saveCachedFilesListing(
      contextA,
      { directory: "" },
      listing("user-a.txt"),
      localStorage,
      firstLease,
    );

    // Broadcast delivery is asynchronous; an old first-handshake closure must
    // not revoke a later lease from the same Worker boot.
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE,
      generation: { bootId: "worker-a", counter: 0 },
      closed: true,
    });
    expect(offlineFiles.isOfflineFileCacheLeaseCurrent(firstLease, contextA)).toBe(true);

    const freshness = await prepare(generation);
    const secondLeasePromise = offlineFiles.setOfflineFileCacheContext(
      contextA,
      freshness,
    );
    const contextMessage = await nextPostedMessage();
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
      requestId: contextMessage.requestId,
      generation,
      accepted: true,
      closed: false,
      transitionComplete: false,
    });
    const secondLease = await secondLeasePromise;

    expect(secondLease).not.toBeNull();
    expect(
      offlineFiles.loadCachedFilesListing(
        contextA,
        { directory: "" },
        localStorage,
        secondLease!,
      )?.data.items[0]?.name,
    ).toBe("user-a.txt");
    expect(
      offlineFiles.loadCachedFilesListing(
        contextB,
        { directory: "" },
        localStorage,
        secondLease!,
      ),
    ).toBeNull();
  });

  it("does not claim global closure after an ACK timeout and rejects late old writes", async () => {
    vi.useFakeTimers();
    const generation = { bootId: "worker-a", counter: 1 };
    const oldLease = await grantInitialLease(generation);
    offlineFiles.saveCachedFilesListing(
      contextA,
      { directory: "" },
      listing("before-timeout.txt"),
      localStorage,
      oldLease,
    );

    const transitionPromise = offlineFiles.beginOfflineFileCacheTransition();
    const begin = await nextPostedMessage();
    expect(begin.type).toBe(offlineFiles.OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE);
    await vi.advanceTimersByTimeAsync(
      offlineFiles.OFFLINE_FILE_CACHE_REPLY_TIMEOUT_MS,
    );
    const transition = await transitionPromise;

    expect(transition.workerAcknowledged).toBe(false);
    expect(offlineFiles.isOfflineFileCacheLeaseCurrent(oldLease, contextA)).toBe(false);
    expect(localStorage.length).toBeGreaterThanOrEqual(1); // durable closed marker
    offlineFiles.saveCachedFilesListing(
      contextA,
      { directory: "" },
      listing("late-old-write.txt"),
      localStorage,
      oldLease,
    );
    expect(
      offlineFiles.loadCachedFilesListing(contextA, { directory: "" }, localStorage, oldLease),
    ).toBeNull();

    // The mutation is intentionally not coupled to the missing ACK. Even if a
    // later probe finds a Worker, the old locally durable transition stays
    // closed until an explicit fresh reconciliation completes it.
    const freshness = await prepare(generation, false);
    expect(offlineFiles.offlineFileCacheFreshnessNeedsTransition(freshness)).toBe(true);
  });

  it("discovers a process restart through its boot nonce without a synthetic invalidation", async () => {
    const beforeRestart = { bootId: "worker-before-restart", counter: 4 };
    const afterRestart = { bootId: "worker-after-restart", counter: 1 };
    const oldLease = await grantInitialLease(beforeRestart);
    offlineFiles.saveCachedFilesListing(
      contextA,
      { directory: "" },
      listing("before-restart.txt"),
      localStorage,
      oldLease,
    );

    // No invalidation message and no controllerchange: only the bounded
    // generation handshake exposes the random new Worker boot identity.
    const freshness = await prepare(afterRestart, true);
    expect(offlineFiles.isOfflineFileCacheLeaseCurrent(oldLease, contextA)).toBe(false);
    expect(
      offlineFiles.loadCachedFilesListing(contextA, { directory: "" }, localStorage, oldLease),
    ).toBeNull();
    expect(offlineFiles.offlineFileCacheFreshnessNeedsTransition(freshness)).toBe(true);

    controllerChange();
    expect(offlineFiles.isOfflineFileCacheLeaseCurrent(oldLease, contextA)).toBe(false);
  });

  it("refuses a lost transition capability rather than silently reopening after restart", async () => {
    const beforeRestart = { bootId: "worker-before-restart", counter: 1 };
    await prepare(beforeRestart);
    const transitionPromise = offlineFiles.beginOfflineFileCacheTransition();
    const begin = await nextPostedMessage();
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE,
      requestId: begin.requestId,
      generation: { bootId: "worker-before-restart", counter: 2 },
      accepted: true,
      closed: true,
      transitionComplete: false,
    });
    const transition = await transitionPromise;

    const restartedFreshness = await prepare(
      { bootId: "worker-after-restart", counter: 1 },
      true,
    );
    expect(
      offlineFiles.offlineFileCacheTransitionWasLost(transition, restartedFreshness),
    ).toBe(true);
    await expect(
      offlineFiles.setOfflineFileCacheContext(
        contextB,
        restartedFreshness,
        transition,
      ),
    ).resolves.toBeNull();
    expect(postedMessages).toEqual([]);
  });
});
