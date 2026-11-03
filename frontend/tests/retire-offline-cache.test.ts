import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import { afterEach, describe, expect, it, vi } from "vitest";

import { retireOfflineCache } from "@/retireOfflineCache";

const retirementScript = readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../public/sw.js"), "utf8",
);

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("one-way removal of the old offline installation", () => {
  it("only removes FrostVault listings and registrations, preserving theme and unrelated data", async () => {
    for (const key of [
      "frostvault.files.cache.v1:listing",
      "frostvault.files.cache.v2:listing",
      "frostvault.files.cache.v3:listing",
      "frostvault.offline-file-cache-barrier.v1",
    ]) localStorage.setItem(key, "old listing");
    localStorage.setItem("frostvault.theme.owner", "dark");
    localStorage.setItem("other-app", "keep");

    const unregister = vi.fn(async () => true);
    const unrelatedUnregister = vi.fn(async () => true);
    const register = vi.fn();
    vi.stubGlobal("navigator", { serviceWorker: {
      register,
      getRegistrations: async () => [
        { active: { scriptURL: `${location.origin}/sw.js?v=old` }, unregister },
        { active: { scriptURL: `${location.origin}/other/sw.js` }, unregister: unrelatedUnregister },
      ],
    } });
    const deleted = vi.fn<(name: string) => Promise<boolean>>(async () => true);
    vi.stubGlobal("caches", {
      keys: async () => [
        "frostvault-file-listing", "frostvault-file-listing-v2:old",
        "frostvault-file-listing-v3:old", `workbox-precache-v2-${location.origin}/`,
        `workbox-precache-v2-${location.origin}/other/`, "other-app",
      ],
      delete: deleted,
    });

    await retireOfflineCache();
    await retireOfflineCache(); // Safe to retry on every new document.
    expect(Object.keys(localStorage).sort()).toEqual(["frostvault.theme.owner", "other-app"]);
    expect(unregister).toHaveBeenCalledTimes(2);
    expect(unrelatedUnregister).not.toHaveBeenCalled();
    expect(register).not.toHaveBeenCalled();
    expect(deleted.mock.calls.map(([name]) => name)).not.toContain("other-app");
    expect(deleted.mock.calls.map(([name]) => name)).not.toContain(`workbox-precache-v2-${location.origin}/other/`);
    expect(deleted).toHaveBeenCalledTimes(8);
  });

  it("does not prevent startup when browser storage or Workers are unavailable", async () => {
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("localStorage", new Proxy({}, { ownKeys() { throw new Error("denied"); } }));
    vi.stubGlobal("caches", { keys: async () => { throw new Error("denied"); } });
    await expect(retireOfflineCache()).resolves.toBeUndefined();
  });

  it.each([false, true])("the legacy URL unregisters and reloads clients (storage failure: %s)", async (storageFailure) => {
    const handlers = new Map<string, (event: { waitUntil(work: Promise<unknown>): void }) => void>();
    const unregister = vi.fn(async () => true);
    const navigate = vi.fn(async () => undefined);
    const skipWaiting = vi.fn(async () => undefined);
    const deleted = vi.fn(async () => true);
    runInNewContext(retirementScript, {
      self: {
        addEventListener: (name: string, handler: typeof handlers extends Map<string, infer H> ? H : never) => handlers.set(name, handler),
        skipWaiting,
        registration: { scope: `${location.origin}/`, unregister },
        clients: { matchAll: async () => [{ url: `${location.origin}/?directory=photos`, navigate }] },
      },
      caches: {
        keys: async () => {
          if (storageFailure) throw new Error("denied");
          return ["frostvault-file-listing-v3:old", "other-app"];
        },
        delete: deleted,
      },
    });
    expect([...handlers.keys()]).toEqual(["install", "activate"]);
    let completion: Promise<unknown> | undefined;
    const event = { waitUntil: (work: Promise<unknown>) => { completion = work; } };
    handlers.get("install")!(event);
    await completion;
    expect(skipWaiting).toHaveBeenCalled();
    handlers.get("activate")!(event);
    await completion;
    expect(unregister).toHaveBeenCalledOnce();
    expect(navigate).toHaveBeenCalledWith(`${location.origin}/?directory=photos`);
    if (!storageFailure) expect(deleted).toHaveBeenCalledExactlyOnceWith("frostvault-file-listing-v3:old");
  });
});
