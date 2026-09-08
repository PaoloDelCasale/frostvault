import { webcrypto } from "node:crypto";
import { cleanup } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";

/**
 * jsdom Blob only implements `slice`/`size`/`type`. `new Response(...).blob()`
 * uses that constructor, so `text()` / `arrayBuffer()` are missing and SHA-256
 * download checks collapse into "browser cannot verify". Patch the jsdom
 * prototype instead of replacing Blob with `node:buffer`, which FileReader
 * rejects (`parameter 1 is not of type 'Blob'`).
 */
function installBinaryTestPrimitives(): void {
  const proto = globalThis.Blob?.prototype;
  if (proto && typeof proto.arrayBuffer !== "function") {
    proto.arrayBuffer = function arrayBuffer(this: Blob): Promise<ArrayBuffer> {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as ArrayBuffer);
        reader.onerror = () =>
          reject(reader.error ?? new Error("Failed to read blob"));
        reader.readAsArrayBuffer(this);
      });
    };
  }
  if (proto && typeof proto.text !== "function") {
    proto.text = async function text(this: Blob): Promise<string> {
      return new TextDecoder().decode(await this.arrayBuffer());
    };
  }
  if (typeof globalThis.crypto?.subtle?.digest !== "function") {
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      writable: true,
      value: webcrypto,
    });
  }
}

installBinaryTestPrimitives();

/**
 * jsdom does not implement EventSource. Provide a minimal injectable stub so
 * production modules that construct one never throw ReferenceError during the
 * full suite. Individual tests may override `globalThis.EventSource`.
 */
class MockEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  readonly url: string;
  readonly withCredentials: boolean;
  readyState = MockEventSource.CONNECTING;
  onerror: ((this: MockEventSource, ev: Event) => unknown) | null = null;
  onmessage: ((this: MockEventSource, ev: MessageEvent) => unknown) | null =
    null;
  onopen: ((this: MockEventSource, ev: Event) => unknown) | null = null;
  private readonly listeners = new Map<
    string,
    Set<(event: MessageEvent<string>) => void>
  >();

  constructor(url: string | URL, init?: EventSourceInit) {
    this.url = String(url);
    this.withCredentials = Boolean(init?.withCredentials);
    this.readyState = MockEventSource.OPEN;
  }

  addEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject | null,
  ): void {
    if (!listener || typeof listener !== "function") return;
    const bucket = this.listeners.get(type) ?? new Set();
    bucket.add(listener as (event: MessageEvent<string>) => void);
    this.listeners.set(type, bucket);
  }

  removeEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject | null,
  ): void {
    if (!listener || typeof listener !== "function") return;
    this.listeners.get(type)?.delete(listener as (event: MessageEvent<string>) => void);
  }

  close(): void {
    this.readyState = MockEventSource.CLOSED;
    this.listeners.clear();
  }

  /** Test helper: dispatch a named SSE frame. */
  emit(type: string, data: string): void {
    const event = { data } as MessageEvent<string>;
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
    if (type === "message" && this.onmessage) {
      this.onmessage.call(this, event);
    }
  }
}

// Prefer defineProperty so later imports always see a constructable global.
Object.defineProperty(globalThis, "EventSource", {
  configurable: true,
  writable: true,
  value: MockEventSource,
});
vi.stubGlobal("EventSource", MockEventSource);

if (typeof globalThis.ResizeObserver === "undefined") {
  class MockResizeObserver {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
}

afterEach(() => {
  cleanup();
});
