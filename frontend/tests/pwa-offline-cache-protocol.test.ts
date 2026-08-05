import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FilesResponse } from "@/api/types";

type OfflineFilesModule = typeof import("@/pwa/offlineFiles");
type ServiceWorkerMessageListener = (event: MessageEvent<unknown>) => void;
type ControllerChangeListener = () => void;

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

  async function prepare(generation: { bootId: string; counter: number }, closed = false) {
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

  it("keeps the barrier closed through mutation and only grants the post-mutation completion a new lease", async () => {
    const beforeMutation = { bootId: "worker-a", counter: 1 };
    const closedGeneration = { bootId: "worker-a", counter: 2 };
    const reopenedGeneration = { bootId: "worker-a", counter: 3 };
    const oldContext = { userId: 11, vaultId: 101 };
    const newContext = { userId: 11, vaultId: 202 };

    const initialFreshness = await prepare(beforeMutation);
    const initialLeasePromise = offlineFiles.setOfflineFileCacheContext(
      oldContext,
      initialFreshness,
    );
    const initialContext = await nextPostedMessage();
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
      requestId: initialContext.requestId,
      generation: beforeMutation,
      accepted: true,
      closed: false,
      transitionComplete: false,
    });
    const oldLease = await initialLeasePromise;
    expect(oldLease).not.toBeNull();
    offlineFiles.saveCachedFilesListing(
      oldContext,
      { directory: "" },
      listing("old-before-transition.txt"),
      localStorage,
      oldLease ?? undefined,
    );

    const transitionPromise = offlineFiles.beginOfflineFileCacheTransition();
    const begin = await nextPostedMessage();
    expect(begin.type).toBe(
      offlineFiles.OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
    );
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE,
      requestId: begin.requestId,
      generation: closedGeneration,
      accepted: true,
      closed: true,
      transitionComplete: false,
    });
    const transition = await transitionPromise;

    // The old UI's delayed write cannot restore local data while the mutation
    // executes, even though the begin acknowledgement already arrived.
    offlineFiles.saveCachedFilesListing(
      oldContext,
      { directory: "" },
      listing("late-old-write.txt"),
      localStorage,
      oldLease ?? undefined,
    );
    expect(localStorage.length).toBe(0);

    // This /api/me freshness is captured only after the server mutation while
    // the Worker remains closed. The opaque transition id is required to open.
    const postMutationFreshness = await prepare(closedGeneration, true);
    const newLeasePromise = offlineFiles.setOfflineFileCacheContext(
      newContext,
      postMutationFreshness,
      transition,
    );
    const completion = await nextPostedMessage();
    expect(completion).toMatchObject({
      type: offlineFiles.OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
      generation: closedGeneration,
      transitionId: transition.id,
    });
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
      requestId: completion.requestId,
      generation: reopenedGeneration,
      accepted: true,
      closed: false,
      transitionComplete: true,
    });
    const newLease = await newLeasePromise;
    expect(newLease).toMatchObject({
      context: newContext,
      generation: reopenedGeneration,
    });

    offlineFiles.saveCachedFilesListing(
      newContext,
      { directory: "" },
      listing("new-after-transition.txt"),
      localStorage,
      newLease ?? undefined,
    );
    expect(
      offlineFiles.loadCachedFilesListing(
        newContext,
        { directory: "" },
        localStorage,
        newLease ?? undefined,
      )?.data.items[0]?.name,
    ).toBe("new-after-transition.txt");
  });

  it("invalidates leases when a restarted Worker repeats a numeric counter with a new boot nonce", async () => {
    const firstGeneration = { bootId: "worker-before-restart", counter: 1 };
    const restartedGeneration = { bootId: "worker-after-restart", counter: 1 };
    const context = { userId: 11, vaultId: 101 };

    const freshness = await prepare(firstGeneration);
    const leasePromise = offlineFiles.setOfflineFileCacheContext(context, freshness);
    const contextMessage = await nextPostedMessage();
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
      requestId: contextMessage.requestId,
      generation: firstGeneration,
      accepted: true,
      closed: false,
      transitionComplete: false,
    });
    const lease = await leasePromise;
    expect(lease).not.toBeNull();
    offlineFiles.saveCachedFilesListing(
      context,
      { directory: "" },
      listing("before-restart.txt"),
      localStorage,
      lease ?? undefined,
    );

    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE,
      generation: restartedGeneration,
      closed: false,
    });
    expect(
      offlineFiles.isOfflineFileCacheLeaseCurrent(lease!, context),
    ).toBe(false);
    expect(localStorage.length).toBe(0);

    // controllerchange also invalidates before a new generation reply arrives.
    controllerChange();
    expect(
      offlineFiles.isOfflineFileCacheLeaseCurrent(lease!, context),
    ).toBe(false);
  });

  it("bounds absent acknowledgements and leaves the client network-only with local data purged", async () => {
    vi.useFakeTimers();
    const generation = { bootId: "worker-without-acks", counter: 1 };
    const context = { userId: 11, vaultId: 101 };

    const freshness = await prepare(generation);
    const leasePromise = offlineFiles.setOfflineFileCacheContext(context, freshness);
    await nextPostedMessage();
    await vi.advanceTimersByTimeAsync(
      offlineFiles.OFFLINE_FILE_CACHE_REPLY_TIMEOUT_MS,
    );
    await expect(leasePromise).resolves.toBeNull();
    expect(localStorage.length).toBe(0);

    const transitionPromise = offlineFiles.beginOfflineFileCacheTransition();
    await nextPostedMessage();
    await vi.advanceTimersByTimeAsync(
      offlineFiles.OFFLINE_FILE_CACHE_REPLY_TIMEOUT_MS,
    );
    await expect(transitionPromise).resolves.toMatchObject({ id: expect.any(String) });
  });
});
