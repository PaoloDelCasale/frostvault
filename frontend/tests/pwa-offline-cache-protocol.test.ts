import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type OfflineFilesModule = typeof import("@/pwa/offlineFiles");

type ServiceWorkerMessageListener = (event: MessageEvent<unknown>) => void;

function listing(name: string) {
  return {
    items: [
      {
        type: "file" as const,
        name,
        path: name,
        state: "both" as const,
        local_size: 12,
      },
    ],
    total: 1,
    page: 1,
    directory: "",
    mode: "browse" as const,
  };
}

describe("offline file-cache client protocol", () => {
  let offlineFiles: OfflineFilesModule;
  let postedMessages: Array<Record<string, unknown>>;
  let messageListeners: Set<ServiceWorkerMessageListener>;

  beforeEach(async () => {
    vi.resetModules();
    localStorage.clear();
    postedMessages = [];
    messageListeners = new Set();
    const worker = {
      postMessage: vi.fn((message: Record<string, unknown>) => {
        postedMessages.push(message);
      }),
    };
    vi.stubGlobal("navigator", {
      ...navigator,
      serviceWorker: {
        controller: worker,
        addEventListener: (
          type: string,
          listener: ServiceWorkerMessageListener,
        ) => {
          if (type === "message") messageListeners.add(listener);
        },
        removeEventListener: (
          type: string,
          listener: ServiceWorkerMessageListener,
        ) => {
          if (type === "message") messageListeners.delete(listener);
        },
        getRegistration: vi.fn(async () => undefined),
      },
    });
    offlineFiles = await import("@/pwa/offlineFiles");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  function deliver(data: unknown): void {
    const event = new MessageEvent("message", { data });
    for (const listener of messageListeners) listener(event);
  }

  async function nextPostedMessage(): Promise<Record<string, unknown>> {
    for (let attempt = 0; attempt < 10; attempt += 1) {
      const message = postedMessages.shift();
      if (message) return message;
      await Promise.resolve();
    }
    throw new Error("client did not post a Service Worker message");
  }

  it("holds clearOfflineFileCache at the Worker acknowledgement barrier and rejects a delayed old lease write", async () => {
    const context = { userId: 11, vaultId: 101 };

    const freshnessPromise = offlineFiles.prepareOfflineFileCacheContext();
    const epochRequest = await nextPostedMessage();
    expect(epochRequest.type).toBe(
      offlineFiles.OFFLINE_FILE_CACHE_EPOCH_REQUEST_MESSAGE,
    );
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_EPOCH_MESSAGE,
      requestId: epochRequest.requestId,
      epoch: 0,
    });
    const freshness = await freshnessPromise;

    const leasePromise = offlineFiles.setOfflineFileCacheContext(
      context,
      freshness,
    );
    const contextRequest = await nextPostedMessage();
    expect(contextRequest.type).toBe(
      offlineFiles.OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
    );
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
      requestId: contextRequest.requestId,
      epoch: 0,
      accepted: true,
    });
    const lease = await leasePromise;
    expect(lease).not.toBeNull();

    const barrier = offlineFiles.clearOfflineFileCache();
    const clearRequest = await nextPostedMessage();
    expect(clearRequest.type).toBe(
      offlineFiles.CLEAR_OFFLINE_FILE_CACHE_MESSAGE,
    );

    // This models a delayed query response from the pre-clear FileBrowser.
    // Its lease was invalidated synchronously, before the Worker acknowledges.
    offlineFiles.saveCachedFilesListing(
      context,
      { directory: "", page: 1, page_size: 100 },
      listing("late-old-payload.txt"),
      localStorage,
      lease ?? undefined,
    );
    expect(localStorage.length).toBe(0);

    let settled = false;
    void barrier.then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);

    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE,
      requestId: clearRequest.requestId,
      epoch: 1,
    });
    await barrier;
    expect(settled).toBe(true);
  });

  it("refuses a /api/me freshness record when a newer Worker epoch arrives first", async () => {
    const freshnessPromise = offlineFiles.prepareOfflineFileCacheContext();
    const epochRequest = await nextPostedMessage();
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_EPOCH_MESSAGE,
      requestId: epochRequest.requestId,
      epoch: 0,
    });
    const staleFreshness = await freshnessPromise;

    // This is the cross-tab clear that races a previously started /api/me.
    // The later context activation must not use its now-stale response.
    deliver({
      type: offlineFiles.OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE,
      epoch: 1,
    });
    await expect(
      offlineFiles.setOfflineFileCacheContext(
        { userId: 11, vaultId: 101 },
        staleFreshness,
      ),
    ).resolves.toBeNull();
    expect(postedMessages).toEqual([]);
  });
});
