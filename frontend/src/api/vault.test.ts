import { afterEach, describe, expect, it, vi } from "vitest";
import { configureApiClient, resetApiClientForTests } from "./client";
import { vaultApi } from "./vault";
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
afterEach(resetApiClientForTests);
describe("immutable Vault request scope", () => {
  it("keeps independent scopes for same-path concurrent operations", async () => {
    const calls: string[] = [];
    configureApiClient({ fetch: vi.fn(async (url) => { calls.push(String(url)); return json({}); }) });
    const first = vaultApi(1), second = vaultApi(2);
    await Promise.all([first.details("same.jpg"), second.details("same.jpg"), second.action("free-space", { path: "same.jpg" })]);
    expect(calls).toEqual(["/api/vaults/1/files/detail?path=same.jpg", "/api/vaults/2/files/detail?path=same.jpg", "/api/vaults/2/free-space"]);
    expect(() => vaultApi(0)).toThrow();
  });
  it("retries a destructive request after reauth with the original identity and CSRF, never Session selection", async () => {
    let sent = 0;
    const calls: { url: string; csrf: string | null }[] = [];
    configureApiClient({ csrfToken: "csrf", getAuthMethod: () => "local", requestPassword: async () => "test-password", fetch: vi.fn(async (url, init) => {
      calls.push({ url: String(url), csrf: new Headers(init?.headers).get("X-CSRF-Token") });
      if (/^\/api\/vaults\/\d+\/cloud-purge/.test(String(url)) && sent++ === 0) return json({ code: "reauth_required" }, 403);
      return json({});
    }) });
    await vaultApi(2).cloudPurge({ path: "same.jpg", confirmation: "Second", reason: "test" });
    expect(calls).toEqual([{ url: "/api/vaults/2/cloud-purge", csrf: "csrf" }, { url: "/api/reauth", csrf: "csrf" }, { url: "/api/vaults/2/cloud-purge", csrf: "csrf" }]);
  });
});
