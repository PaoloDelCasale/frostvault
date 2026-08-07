import { cleanup, render, waitFor } from "@testing-library/react";
import {
  QueryClient,
  QueryClientProvider,
  useQueryClient,
} from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEffect, useState, type ReactNode } from "react";

import {
  apiQueryKeys,
  openCatalogEventSource,
  parseCatalogEventData,
  useCatalogEvents,
  type CatalogEventSourceFactory,
  type CatalogRevisionSignal,
} from "@/api";

type MessageListener = (event: MessageEvent<string>) => void;

function createQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
}

function Harness({
  vaultId,
  createSource,
  fetchRevision,
  schedule,
  cancelSchedule,
  onReady,
}: {
  vaultId: number | null;
  createSource?: CatalogEventSourceFactory;
  fetchRevision?: (
    afterRevision?: number,
    init?: RequestInit,
  ) => Promise<CatalogRevisionSignal>;
  schedule?: (callback: () => void, ms: number) => number;
  cancelSchedule?: (id: number) => void;
  onReady?: (client: QueryClient) => void;
}) {
  const queryClient = useQueryClient();
  useEffect(() => {
    onReady?.(queryClient);
  }, [onReady, queryClient]);
  useCatalogEvents({
    vaultId,
    queryClient,
    createSource,
    fetchRevision,
    schedule,
    cancelSchedule,
  });
  return null;
}

function renderWithClient(client: QueryClient, ui: ReactNode) {
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}

describe("catalog event helpers", () => {
  it("parses catalog frames and ignores malformed payloads", () => {
    expect(
      parseCatalogEventData(
        JSON.stringify({
          vault_id: 9,
          revision: 4,
          domains: ["files", "stats"],
          has_gap: false,
        }),
      ),
    ).toEqual({
      vault_id: 9,
      revision: 4,
      domains: ["files", "stats"],
      has_gap: false,
    });
    expect(parseCatalogEventData("not-json")).toBeNull();
  });
});

describe("useCatalogEvents", () => {
  afterEach(() => {
    cleanup();
  });

  it("invalidates files/stats/rename queries on catalog signals", async () => {
    const client = createQueryClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const catalogHandlers: MessageListener[] = [];
    const createSource: CatalogEventSourceFactory = () => ({
      close: vi.fn(),
      addEventListener: (type, listener) => {
        if (type === "catalog") catalogHandlers.push(listener);
      },
      onerror: null,
    });

    renderWithClient(
      client,
      <Harness vaultId={1} createSource={createSource} />,
    );

    await waitFor(() => expect(catalogHandlers.length).toBeGreaterThan(0));
    catalogHandlers[0]!({
      data: JSON.stringify({
        vault_id: 1,
        revision: 3,
        domains: ["files", "stats", "rename_candidates"],
        has_gap: false,
      }),
    } as MessageEvent<string>);

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith(
        expect.objectContaining({ queryKey: ["files"] }),
      );
      expect(invalidateSpy).toHaveBeenCalledWith(
        expect.objectContaining({ queryKey: apiQueryKeys.stats }),
      );
      expect(invalidateSpy).toHaveBeenCalledWith(
        expect.objectContaining({ queryKey: ["rename-candidates"] }),
      );
    });
  });

  it("ignores out-of-order/duplicate revisions and foreign vaults", async () => {
    const client = createQueryClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const catalogHandlers: MessageListener[] = [];
    const createSource: CatalogEventSourceFactory = () => ({
      close: vi.fn(),
      addEventListener: (type, listener) => {
        if (type === "catalog") catalogHandlers.push(listener);
      },
      onerror: null,
    });

    renderWithClient(
      client,
      <Harness vaultId={1} createSource={createSource} />,
    );
    await waitFor(() => expect(catalogHandlers.length).toBeGreaterThan(0));

    const emit = (payload: object) =>
      catalogHandlers[0]!({
        data: JSON.stringify(payload),
      } as MessageEvent<string>);

    emit({
      vault_id: 1,
      revision: 5,
      domains: ["files"],
      has_gap: false,
    });
    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledTimes(1));

    emit({
      vault_id: 1,
      revision: 5,
      domains: ["files"],
      has_gap: false,
    });
    emit({
      vault_id: 1,
      revision: 4,
      domains: ["stats"],
      has_gap: false,
    });
    emit({
      vault_id: 99,
      revision: 99,
      domains: ["files"],
      has_gap: false,
    });
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
  });

  it("closes the previous stream on vault switch and does not leak invalidations", async () => {
    const client = createQueryClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const closes: Array<ReturnType<typeof vi.fn>> = [];
    const handlers: MessageListener[][] = [];
    const createSource: CatalogEventSourceFactory = () => {
      const close = vi.fn();
      closes.push(close);
      const catalogHandlers: MessageListener[] = [];
      handlers.push(catalogHandlers);
      return {
        close,
        addEventListener: (type, listener) => {
          if (type === "catalog") catalogHandlers.push(listener);
        },
        onerror: null,
      };
    };

    function Switcher() {
      const [vaultId, setVaultId] = useState(1);
      useEffect(() => {
        setVaultId(2);
      }, []);
      return <Harness vaultId={vaultId} createSource={createSource} />;
    }

    renderWithClient(client, <Switcher />);
    await waitFor(() => expect(closes.length).toBeGreaterThanOrEqual(2));
    expect(closes[0]).toHaveBeenCalled();

    handlers[0]?.[0]?.({
      data: JSON.stringify({
        vault_id: 1,
        revision: 8,
        domains: ["files"],
        has_gap: false,
      }),
    } as MessageEvent<string>);
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("reconnects with backoff after connection errors and catches up on focus", async () => {
    const client = createQueryClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const sources: Array<{
      close: ReturnType<typeof vi.fn>;
      onerror: ((ev: Event) => unknown) | null;
    }> = [];
    const createSource: CatalogEventSourceFactory = () => {
      const source = {
        close: vi.fn(),
        addEventListener: vi.fn(),
        onerror: null as ((ev: Event) => unknown) | null,
      };
      sources.push(source);
      return source;
    };
    const fetchRevision = vi.fn(
      async (): Promise<CatalogRevisionSignal> => ({
        vault_id: 1,
        revision: 6,
        domains: ["files", "stats"],
        has_gap: false,
        changed: true,
      }),
    );
    const timers: Array<{ id: number; cb: () => void; ms: number }> = [];
    let nextTimerId = 1;
    const schedule = (callback: () => void, ms: number) => {
      const id = nextTimerId++;
      timers.push({ id, cb: callback, ms });
      return id;
    };
    const cancelSchedule = (id: number) => {
      const index = timers.findIndex((timer) => timer.id === id);
      if (index >= 0) timers.splice(index, 1);
    };

    renderWithClient(
      client,
      <Harness
        vaultId={1}
        createSource={createSource}
        fetchRevision={fetchRevision}
        schedule={schedule}
        cancelSchedule={cancelSchedule}
      />,
    );

    await waitFor(() => expect(sources.length).toBe(1));
    sources[0]!.onerror?.(new Event("error"));
    expect(timers.length).toBe(1);
    expect(timers[0]!.ms).toBe(1_000);
    timers.shift()!.cb();
    await waitFor(() => expect(sources.length).toBe(2));

    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(fetchRevision).toHaveBeenCalled());
    await waitFor(() => expect(invalidateSpy).toHaveBeenCalled());
    expect(timers.every((timer) => timer.ms >= 1_000)).toBe(true);
  });

  it("openCatalogEventSource wires hello/catalog/error listeners", () => {
    const listeners = new Map<string, MessageListener>();
    const source = {
      close: vi.fn(),
      addEventListener: (type: string, listener: MessageListener) => {
        listeners.set(type, listener);
      },
      onerror: null as ((ev: Event) => unknown) | null,
    };
    const onHello = vi.fn();
    const onCatalog = vi.fn();
    const onError = vi.fn();
    const handle = openCatalogEventSource(
      2,
      { onHello, onCatalog, onError },
      () => source,
    );

    listeners.get("hello")?.({
      data: JSON.stringify({ vault_id: 1, revision: 2 }),
    } as MessageEvent<string>);
    listeners.get("catalog")?.({
      data: JSON.stringify({
        vault_id: 1,
        revision: 3,
        domains: ["files"],
        has_gap: false,
      }),
    } as MessageEvent<string>);
    listeners.get("error")?.({
      data: JSON.stringify({ error: "vault_switched", vault_id: 1 }),
    } as MessageEvent<string>);

    expect(onHello).toHaveBeenCalledWith({ vault_id: 1, revision: 2 });
    expect(onCatalog).toHaveBeenCalledWith({
      vault_id: 1,
      revision: 3,
      domains: ["files"],
      has_gap: false,
    });
    expect(onError).toHaveBeenCalledWith({
      error: "vault_switched",
      vault_id: 1,
    });
    handle.close();
    expect(source.close).toHaveBeenCalled();
  });
});
