import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearPushPermissionDenied,
  ensurePushSubscription,
  rememberPushPermissionDenied,
  wasPushPermissionDenied,
} from "@/pwa";
import {
  clearOfflineFileCache,
  loadCachedFilesListing,
  saveCachedFilesListing,
} from "@/pwa/offlineFiles";
import type { FilesResponse } from "@/api/types";

function memoryStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear() {
      map.clear();
    },
    getItem(key: string) {
      return map.has(key) ? map.get(key)! : null;
    },
    key(index: number) {
      return [...map.keys()][index] ?? null;
    },
    removeItem(key: string) {
      map.delete(key);
    },
    setItem(key: string, value: string) {
      map.set(key, value);
    },
  };
}

const sampleListing: FilesResponse = {
  mode: "browse",
  directory: "",
  page: 1,
  total: 1,
  items: [
    {
      type: "file",
      path: "readme.txt",
      name: "readme.txt",
      state: "both",
      local_size: 12,
    },
  ],
};

describe("offline file listing cache (seam 3)", () => {
  const query = { directory: "" };
  const userAVaultA = { userId: 11, vaultId: 101 };
  const userBVaultA = { userId: 22, vaultId: 101 };
  const userAVaultB = { userId: 11, vaultId: 202 };

  function listingFor(path: string): FilesResponse {
    return {
      ...sampleListing,
      items: [{ ...sampleListing.items[0]!, name: path, path }],
    };
  }

  it("returns cached listing and marks it available for stale UI", () => {
    const storage = memoryStorage();
    saveCachedFilesListing(userAVaultA, query, sampleListing, storage);
    const cached = loadCachedFilesListing(userAVaultA, query, storage);
    expect(cached?.data.items[0]?.path).toBe("readme.txt");
    expect(cached?.savedAt).toMatch(/^\d{4}-/);
  });

  it("purges User A's listing on logout before User B can cache or load files", () => {
    const storage = memoryStorage();
    saveCachedFilesListing(userAVaultA, query, listingFor("user-a.txt"), storage);

    // User A signs out. The same browser then authenticates User B.
    clearOfflineFileCache(storage);
    saveCachedFilesListing(userBVaultA, query, listingFor("user-b.txt"), storage);

    expect(loadCachedFilesListing(userAVaultA, query, storage)).toBeNull();
    expect(loadCachedFilesListing(userBVaultA, query, storage)?.data.items[0]?.path).toBe(
      "user-b.txt",
    );
  });

  it("does not reuse Vault A's listing after the same User switches to Vault B", () => {
    const storage = memoryStorage();
    saveCachedFilesListing(userAVaultA, query, listingFor("vault-a.txt"), storage);

    // Vault selection changes before the next file listing is requested.
    expect(loadCachedFilesListing(userAVaultB, query, storage)).toBeNull();
    clearOfflineFileCache(storage);
    saveCachedFilesListing(userAVaultB, query, listingFor("vault-b.txt"), storage);

    expect(loadCachedFilesListing(userAVaultA, query, storage)).toBeNull();
    expect(loadCachedFilesListing(userAVaultB, query, storage)?.data.items[0]?.path).toBe(
      "vault-b.txt",
    );
  });

  it("invalidates legacy query-only entries instead of reusing them", () => {
    const storage = memoryStorage();
    storage.setItem(
      "frostvault.files.cache.v1:|||1|100",
      JSON.stringify({ data: listingFor("legacy.txt"), savedAt: "2026-01-01T00:00:00Z" }),
    );

    expect(loadCachedFilesListing(userAVaultA, query, storage)).toBeNull();
    expect(storage.length).toBe(0);
  });
});

describe("push permission (seam 8)", () => {
  beforeEach(() => {
    clearPushPermissionDenied(memoryStorage());
  });

  it("does not ask again after permission was denied", async () => {
    const storage = memoryStorage();
    rememberPushPermissionDenied(storage);
    expect(wasPushPermissionDenied(storage)).toBe(true);

    const requestPermission = vi.fn(async () => "denied" as NotificationPermission);
    const result = await ensurePushSubscription({
      storage,
      fetchConfig: async () => ({
        configured: true,
        vapid_public_key: "BPtest",
      }),
      requestPermission,
    });
    expect(result).toBe("denied");
    expect(requestPermission).not.toHaveBeenCalled();
  });

  it("skips quietly when push is not configured (seam 7)", async () => {
    const storage = memoryStorage();
    const result = await ensurePushSubscription({
      storage,
      fetchConfig: async () => ({ configured: false, vapid_public_key: null }),
      requestPermission: vi.fn(async () => "granted" as NotificationPermission),
    });
    expect(result).toBe("skipped");
  });
});
