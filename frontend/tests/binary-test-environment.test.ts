import { describe, expect, it } from "vitest";

describe("binary test primitives", () => {
  it("exposes a coherent Blob, Response, and WebCrypto family", async () => {
    const blob = new Blob(["probe"], { type: "application/octet-stream" });
    expect(typeof blob.text).toBe("function");
    expect(typeof blob.arrayBuffer).toBe("function");
    expect(await blob.text()).toBe("probe");
    expect(Array.from(new Uint8Array(await blob.arrayBuffer()))).toEqual(
      Array.from(new TextEncoder().encode("probe")),
    );

    const fromResponse = await new Response("bytes").blob();
    expect(typeof fromResponse.text).toBe("function");
    expect(typeof fromResponse.arrayBuffer).toBe("function");
    expect(await fromResponse.text()).toBe("bytes");

    expect(typeof globalThis.crypto?.subtle?.digest).toBe("function");
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode("probe"),
    );
    expect(digest.byteLength).toBe(32);
  });
});
