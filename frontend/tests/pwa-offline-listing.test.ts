import { describe, expect, it } from "vitest";
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
  const userAVaultA = {
    userId: 11,
    vaultId: 101,
    authorizationGeneration: "session-a-vault-a",
  };
  const userBVaultA = {
    userId: 22,
    vaultId: 101,
    authorizationGeneration: "session-b-vault-a",
  };
  const userAVaultB = {
    userId: 11,
    vaultId: 202,
    authorizationGeneration: "session-a-vault-b",
  };

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

  it("purges User A's listing on logout before User B can cache or load files", async () => {
    const storage = memoryStorage();
    saveCachedFilesListing(userAVaultA, query, listingFor("user-a.txt"), storage);

    // User A signs out. The same browser then authenticates User B.
    await clearOfflineFileCache(storage);
    saveCachedFilesListing(userBVaultA, query, listingFor("user-b.txt"), storage);

    expect(loadCachedFilesListing(userAVaultA, query, storage)).toBeNull();
    expect(loadCachedFilesListing(userBVaultA, query, storage)?.data.items[0]?.path).toBe(
      "user-b.txt",
    );
  });

  it("does not reuse Vault A's listing after the same User switches to Vault B", async () => {
    const storage = memoryStorage();
    saveCachedFilesListing(userAVaultA, query, listingFor("vault-a.txt"), storage);

    // Vault selection changes before the next file listing is requested.
    expect(loadCachedFilesListing(userAVaultB, query, storage)).toBeNull();
    await clearOfflineFileCache(storage);
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
