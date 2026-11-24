import { act, cleanup, waitFor } from "@testing-library/react";
import {
  type QueryClient,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEffect, useState, type ReactNode } from "react";

import { CATALOG_REFRESH_MS, STATS_REFRESH_MS, apiQueryKeys, catalogEventsUrl, catalogRevisionUrl, openCatalogEventSource, parseCatalogEventData, useCatalogEvents, type CatalogEventSourceFactory, type CatalogRevisionSignal } from "@/api";
import { createTestQueryClient, renderWithQueryClient } from "./support/queryClient";

type MessageListener = (event: MessageEvent<string>) => void;

function createQueryClient() {
  return createTestQueryClient({
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
  onSessionChanged,
}: {
  vaultId: number | null;
  createSource?: CatalogEventSourceFactory;
  fetchRevision?: (
    afterRevision: number,
    init: RequestInit | undefined,
    vaultId: number,
  ) => Promise<CatalogRevisionSignal>;
  schedule?: (callback: () => void, ms: number) => number;
  cancelSchedule?: (id: number) => void;
  onReady?: (client: QueryClient) => void;
  onSessionChanged?: () => void;
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
    onSessionChanged,
  });
  return null;
}

function renderWithClient(client: QueryClient, ui: ReactNode) {
  return renderWithQueryClient(ui, { client });
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

  it("names the Vault in the path of the stream and of the catch-up (ADR-0056)", () => {
    expect(catalogEventsUrl(0, 7)).toBe("/api/vaults/7/catalog/events");
    expect(catalogEventsUrl(12, 7)).toBe("/api/vaults/7/catalog/events?after_revision=12");
    expect(catalogRevisionUrl(0, 7)).toBe("/api/vaults/7/catalog/revision");
    expect(catalogRevisionUrl(12, 7)).toBe("/api/vaults/7/catalog/revision?after_revision=12");
  });
});

describe("useCatalogEvents", () => {
  afterEach(() => {
    cleanup();
  });

  it("invalidates files/stats queries on catalog signals", async () => {
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
        domains: ["files", "stats"],
        has_gap: false,
      }),
    } as MessageEvent<string>);

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith(
        expect.objectContaining({ queryKey: ["files"] }),
        { cancelRefetch: false },
      );
      expect(invalidateSpy).toHaveBeenCalledWith(
        expect.objectContaining({ queryKey: ["vault-stats", 1], exact: true }),
        { cancelRefetch: false },
      );
    });
  });

  it("refetches only the event Vault's mounted stats query", async () => {
    const client = createQueryClient();
    const catalogHandlers: MessageListener[] = [];
    const createSource: CatalogEventSourceFactory = () => ({
      close: vi.fn(),
      addEventListener: (type, listener) => {
        if (type === "catalog") catalogHandlers.push(listener);
      },
      onerror: null,
    });
    const fetched = { 1: 0, 2: 0 };
    function StatsProbe({ id }: { id: 1 | 2 }) {
      useQuery({
        queryKey: apiQueryKeys.vaultStats(id),
        queryFn: () => ++fetched[id],
        staleTime: Infinity,
      });
      return null;
    }

    renderWithClient(
      client,
      <>
        <StatsProbe id={1} />
        <StatsProbe id={2} />
        <Harness vaultId={1} createSource={createSource} />
      </>,
    );

    await waitFor(() => {
      expect(fetched).toEqual({ 1: 1, 2: 1 });
      expect(catalogHandlers.length).toBeGreaterThan(0);
    });
    catalogHandlers[0]!({
      data: JSON.stringify({ vault_id: 1, revision: 5, domains: ["stats"], has_gap: false }),
    } as MessageEvent<string>);

    await waitFor(() => expect(fetched[1]).toBe(2));
    expect(fetched[2]).toBe(1);
  });

  it("refreshes stats at once, then at most every 30 s under continuous events", async () => {
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
    const timers: Array<{ cb: () => void; ms: number }> = [];
    const schedule = (callback: () => void, ms: number) => timers.push({ cb: callback, ms });
    const statsCalls = () => invalidateSpy.mock.calls
      .filter(([filters]) => filters?.queryKey?.[0] === "vault-stats").length;
    const emit = (revision: number) => catalogHandlers[0]!({
      data: JSON.stringify({ vault_id: 1, revision, domains: ["files", "stats"], has_gap: false }),
    } as MessageEvent<string>);
    const fire = (ms: number) => {
      const index = timers.findIndex((timer) => timer.ms === ms);
      expect(index).toBeGreaterThanOrEqual(0);
      timers.splice(index, 1)[0]!.cb();
    };

    renderWithClient(
      client,
      <Harness vaultId={1} createSource={createSource} schedule={schedule} cancelSchedule={() => {}} />,
    );
    await waitFor(() => expect(catalogHandlers.length).toBeGreaterThan(0));

    emit(1);
    expect(statsCalls()).toBe(1);

    // Continuous activity: files keep the 5 s cadence, stats wait for their window.
    for (let revision = 2; revision <= 4; revision += 1) {
      emit(revision);
      fire(CATALOG_REFRESH_MS);
    }
    expect(statsCalls()).toBe(1);
    const trailing = timers.filter((timer) => timer.ms > CATALOG_REFRESH_MS);
    expect(trailing).toHaveLength(1);
    expect(trailing[0]!.ms).toBeLessThanOrEqual(STATS_REFRESH_MS);

    trailing[0]!.cb();
    expect(statsCalls()).toBe(2);
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

  it("opens, resumes and catches up on the address of its own Vault", async () => {
    const client = createQueryClient();
    const opened: string[] = [];
    const sources: Array<{ catalog?: MessageListener; onerror: ((ev: Event) => unknown) | null }> = [];
    const createSource: CatalogEventSourceFactory = (url) => {
      opened.push(url);
      const source: (typeof sources)[number] & { close: () => void; addEventListener: (type: string, listener: MessageListener) => void } = {
        close: vi.fn(), onerror: null,
        addEventListener: (type, listener) => { if (type === "catalog") source.catalog = listener; },
      };
      sources.push(source);
      return source;
    };
    const fetchRevision = vi.fn(async (_after: number, _init: RequestInit | undefined, vaultId: number): Promise<CatalogRevisionSignal> => (
      { vault_id: vaultId, revision: 4, domains: ["files"], has_gap: false, changed: false }));
    const timers: Array<{ cb: () => void; ms: number }> = [];
    const schedule = (cb: () => void, ms: number) => timers.push({ cb, ms });
    const cancelSchedule = () => undefined;
    let openVault: (vaultId: number) => void = () => undefined;
    function Switcher() {
      const [vaultId, setVaultId] = useState(1);
      openVault = setVaultId;
      return <Harness vaultId={vaultId} createSource={createSource} fetchRevision={fetchRevision} schedule={schedule} cancelSchedule={cancelSchedule} />;
    }
    renderWithClient(client, <Switcher />);
    await waitFor(() => expect(sources.length).toBe(1));
    sources[0]!.catalog!({ data: JSON.stringify({ vault_id: 1, revision: 4, domains: ["files"], has_gap: false }) } as MessageEvent<string>);
    sources[0]!.onerror?.(new Event("error"));
    timers.find((timer) => timer.ms === 1_000)!.cb();
    await waitFor(() => expect(sources.length).toBe(2));
    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(fetchRevision).toHaveBeenCalledWith(4, undefined, 1));
    // Another Vault: its own address, from its own first revision.
    act(() => openVault(2));
    await waitFor(() => expect(sources.length).toBe(3));
    expect(opened).toEqual([
      "/api/vaults/1/catalog/events",
      "/api/vaults/1/catalog/events?after_revision=4",
      "/api/vaults/2/catalog/events",
    ]);
    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(fetchRevision).toHaveBeenLastCalledWith(0, undefined, 2));
  });

  it.each([
    "vault_access_revoked", "session_revoked",
    "session_expired", "session_version_mismatch",
  ])("refreshes the online document on a durable %s event", async (error) => {
    const client = createQueryClient();
    const onSessionChanged = vi.fn();
    const listeners = new Map<string, MessageListener>();
    const close = vi.fn();
    const createSource: CatalogEventSourceFactory = () => ({
      close, onerror: null,
      addEventListener: (type, listener) => { listeners.set(type, listener); },
    });
    renderWithClient(client, <Harness vaultId={1} createSource={createSource} onSessionChanged={onSessionChanged} />);
    await waitFor(() => expect(listeners.has("error")).toBe(true));
    listeners.get("error")!({ data: JSON.stringify({ error, vault_id: 1 }) } as MessageEvent<string>);
    expect(close).toHaveBeenCalled();
    expect(onSessionChanged).toHaveBeenCalledOnce();
    // Duplicate/in-flight signals must not restart work for the old Vault.
    listeners.get("error")!({ data: JSON.stringify({ error, vault_id: 1 }) } as MessageEvent<string>);
    expect(onSessionChanged).toHaveBeenCalledOnce();
  });

  it("reconciles a Vault switch discovered on focus even with no changed catalog rows", async () => {
    const client = createQueryClient();
    const onSessionChanged = vi.fn();
    const createSource: CatalogEventSourceFactory = () => ({ close: vi.fn(), onerror: null, addEventListener: vi.fn() });
    const fetchRevision = vi.fn(async (): Promise<CatalogRevisionSignal> => ({
      vault_id: 2, revision: 0, domains: [], has_gap: false, changed: false,
    }));
    renderWithClient(client, <Harness vaultId={1} createSource={createSource} fetchRevision={fetchRevision} onSessionChanged={onSessionChanged} />);
    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(onSessionChanged).toHaveBeenCalledOnce());
  });

  it("refreshes when a reconnected stream belongs to a different Vault", async () => {
    const client = createQueryClient();
    const onSessionChanged = vi.fn();
    const listeners = new Map<string, MessageListener>();
    const createSource: CatalogEventSourceFactory = () => ({
      close: vi.fn(), onerror: null,
      addEventListener: (type, listener) => { listeners.set(type, listener); },
    });
    renderWithClient(client, <Harness vaultId={1} createSource={createSource} onSessionChanged={onSessionChanged} />);
    await waitFor(() => expect(listeners.has("hello")).toBe(true));
    listeners.get("hello")!({ data: JSON.stringify({ vault_id: 2, revision: 0 }) } as MessageEvent<string>);
    expect(onSessionChanged).toHaveBeenCalledOnce();
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
    const opened: string[] = [];
    const handle = openCatalogEventSource(
      2,
      { onHello, onCatalog, onError },
      (url) => { opened.push(url); return source; },
      1,
    );
    expect(opened).toEqual(["/api/vaults/1/catalog/events?after_revision=2"]);

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
      data: JSON.stringify({ error: "vault_access_revoked", vault_id: 1 }),
    } as MessageEvent<string>);

    expect(onHello).toHaveBeenCalledWith({ vault_id: 1, revision: 2 });
    expect(onCatalog).toHaveBeenCalledWith({
      vault_id: 1,
      revision: 3,
      domains: ["files"],
      has_gap: false,
    });
    expect(onError).toHaveBeenCalledWith({
      error: "vault_access_revoked",
      vault_id: 1,
    });
    handle.close();
    expect(source.close).toHaveBeenCalled();
  });
});
