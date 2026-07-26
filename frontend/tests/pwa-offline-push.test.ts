import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearPushPermissionDenied,
  ensurePushSubscription,
  loadCachedFilesListing,
  rememberPushPermissionDenied,
  saveCachedFilesListing,
  wasPushPermissionDenied,
} from "@/pwa";
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
  it("returns cached listing and marks it available for stale UI", () => {
    const storage = memoryStorage();
    saveCachedFilesListing({ directory: "" }, sampleListing, storage);
    const cached = loadCachedFilesListing({ directory: "" }, storage);
    expect(cached?.data.items[0]?.path).toBe("readme.txt");
    expect(cached?.savedAt).toMatch(/^\d{4}-/);
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
