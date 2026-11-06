import { act, cleanup, render } from "@testing-library/react";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useCatalogEvents } from "@/api/useCatalogEvents";
import type { CatalogEventSourceFactory } from "@/api/catalogEvents";

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const listeners = new Map<string, (event: MessageEvent<string>) => void>();
  const close = vi.fn();
  const createSource: CatalogEventSourceFactory = () => ({ close, onerror: null, addEventListener: (type, listener) => { listeners.set(type, listener); } });
  function Harness({ vaultId = 1 }: { vaultId?: number }) { useCatalogEvents({ vaultId, queryClient: client, explicitVault: true, createSource }); return null; }
  const view = render(<Harness />);
  const emit = (revision: number, domains = ["files"], has_gap = false) => {
    act(() => listeners.get("catalog")!({ data: JSON.stringify({ vault_id: 1, revision, domains, has_gap }) } as MessageEvent<string>));
  };
  return { client, emit, view, close, Harness };
}
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("catalog refresh coalescing lifecycle", () => {
  it("unions domains/gaps, dedupes revisions, and discards scheduled work on unmount", async () => {
    vi.useFakeTimers();
    const { client, emit, view, close } = setup();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    emit(1); expect(invalidate.mock.calls.some(([filters]) => filters?.queryKey?.[0] === "stats")).toBe(false);
    invalidate.mockClear();
    emit(2, ["stats"]); emit(3, ["files"]); emit(3, ["files"]); emit(2, ["files"]);
    expect(invalidate).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(invalidate.mock.calls.some(([filters]) => filters?.queryKey?.[0] === "stats")).toBe(true);
    expect(invalidate.mock.calls.filter(([filters]) => filters?.queryKey?.[0] === "files")).toHaveLength(1);
    invalidate.mockClear();
    emit(3, ["unknown"], true); // gaps invalidate everything despite duplicate revision
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(invalidate.mock.calls.some(([filters]) => filters?.queryKey?.[0] === "notifications")).toBe(true);
    emit(4); view.unmount(); invalidate.mockClear();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(invalidate).not.toHaveBeenCalled(); expect(close).toHaveBeenCalledOnce(); client.clear();
  });

  it("only refetches active displayed queries, not cached folders or another Vault", async () => {
    vi.useFakeTimers();
    const { client, emit } = setup();
    const files = vi.fn(async () => ({ items: [] }));
    const other = vi.fn(async () => ({ items: [] }));
    const shown = new QueryObserver(client, { queryKey: ["files", 1, "", "", "photos", 1, 0], queryFn: files });
    const foreign = new QueryObserver(client, { queryKey: ["files", 2, "", "", "photos", 1, 0], queryFn: other });
    const unsubscribe = [shown.subscribe(() => {}), foreign.subscribe(() => {})];
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    client.setQueryData(["files", 1, "", "", "unrelated", 1, 0], { items: [] });
    files.mockClear(); other.mockClear(); emit(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(files).toHaveBeenCalledOnce(); expect(other).not.toHaveBeenCalled();
    expect(client.getQueryState(["files", 1, "", "", "unrelated", 1, 0])?.isInvalidated).toBe(true);
    unsubscribe.forEach((stop) => stop()); client.clear();
  });

  it("does not cancel slow requests or lose the trailing change behind their old snapshot", async () => {
    vi.useFakeTimers();
    const { client, emit } = setup();
    let complete!: (value: string) => void;
    const fetch = vi.fn<() => Promise<string>>()
      .mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }))
      .mockResolvedValue("new snapshot");
    const key = ["files", 1, "", "", "photos", 1, 0];
    const observer = new QueryObserver(client, { queryKey: key, queryFn: fetch });
    const unsubscribe = observer.subscribe(() => {});
    emit(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(fetch).toHaveBeenCalledOnce();
    await act(async () => { complete("old snapshot"); await vi.advanceTimersByTimeAsync(1); });
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(client.getQueryData(key)).toBe("new snapshot");
    unsubscribe(); client.clear();
  });

  it("drops the old Vault's trailing invalidation when selection changes", async () => {
    vi.useFakeTimers();
    const { client, emit, view, Harness } = setup();
    emit(1); emit(2);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    view.rerender(<Harness vaultId={2} />);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(invalidate).not.toHaveBeenCalled(); client.clear();
  });
});
