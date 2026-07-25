import { describe, expect, it } from "vitest";

import { createLatestRequestScope } from "./latest";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("createLatestRequestScope", () => {
  // Ported from tests/test_admin_ui.mjs:
  // "out-of-order member success cannot replace the selected vault"
  it("discards an older success in favour of the newest request (out-of-order member success)", async () => {
    const scope = createLatestRequestScope();
    const first = deferred<{ vault: string }>();
    const second = deferred<{ vault: string }>();

    const firstSettle = scope.begin().settle(first.promise);
    const secondHandle = scope.begin();
    const secondSettle = secondHandle.settle(second.promise);

    second.resolve({ vault: "B" });
    first.resolve({ vault: "A" });

    expect(await firstSettle).toBeUndefined();
    expect(await secondSettle).toEqual({ vault: "B" });
    expect(secondHandle.isCurrent()).toBe(true);
  });

  // Ported from: "out-of-order member error cannot replace the selected vault or show an error"
  it("discards an older error so it cannot surface after a newer success (out-of-order member error)", async () => {
    const scope = createLatestRequestScope();
    const first = deferred<{ vault: string }>();
    const second = deferred<{ vault: string }>();

    const firstSettle = scope.begin().settle(first.promise);
    const secondSettle = scope.begin().settle(second.promise);

    second.resolve({ vault: "B" });
    first.reject(new Error("Vault A failed"));

    expect(await firstSettle).toBeUndefined();
    expect(await secondSettle).toEqual({ vault: "B" });
  });

  // Ported from: "stale transfer success cannot mutate the newly selected vault"
  it("ignores a stale mutating success after a newer selection begins (stale transfer success)", async () => {
    const members = createLatestRequestScope();
    const transfers = createLatestRequestScope();

    const loadA = members.begin();
    await loadA.settle(Promise.resolve({ vaultId: 1 }));

    const transfer = deferred<{ ok: boolean }>();
    const transferSettle = transfers.begin().settle(transfer.promise);

    // Newer vault selection invalidates in-flight transfer side effects.
    members.begin();
    transfers.begin();

    transfer.resolve({ ok: true });
    expect(await transferSettle).toBeUndefined();
  });

  // Ported from: "stale transfer error cannot mutate the newly selected vault"
  it("ignores a stale mutating error after a newer selection begins (stale transfer error)", async () => {
    const transfers = createLatestRequestScope();
    const transfer = deferred<never>();
    const transferSettle = transfers.begin().settle(transfer.promise);

    transfers.begin();
    transfer.reject(new Error("Vault A transfer failed"));

    expect(await transferSettle).toBeUndefined();
  });

  // Ported from: "stale quota loads and saves cannot replace the selected vault"
  it("keeps only the newest load/save results for a selection (stale quota loads and saves)", async () => {
    const quotas = createLatestRequestScope();
    const loadA = deferred<{ soft: number }>();
    const loadB = deferred<{ soft: number }>();

    const settleA = quotas.begin().settle(loadA.promise);
    const settleB = quotas.begin().settle(loadB.promise);

    loadB.resolve({ soft: 22 });
    loadA.resolve({ soft: 11 });
    expect(await settleA).toBeUndefined();
    expect(await settleB).toEqual({ soft: 22 });

    const save = deferred<{ soft: number }>();
    const saveSettle = quotas.begin().settle(save.promise);
    quotas.begin(); // selection changed before save completed
    save.resolve({ soft: 999 });
    expect(await saveSettle).toBeUndefined();
  });

  // Ported from: "transfer cannot submit a stale member while a newer vault load is pending"
  it("reports when the current generation has not settled yet (pending newer vault load)", async () => {
    const members = createLatestRequestScope();
    const pending = deferred<{ vaultId: number }>();
    const handle = members.begin();
    const settlePromise = handle.settle(pending.promise);

    expect(handle.isCurrent()).toBe(true);
    expect(members.hasSettledCurrent()).toBe(false);

    pending.resolve({ vaultId: 2 });
    await settlePromise;
    expect(members.hasSettledCurrent()).toBe(true);
  });
});
