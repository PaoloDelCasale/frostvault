import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MeResponse } from "@/api/types";

const protocol = vi.hoisted(() => ({
  begin: vi.fn(),
  finish: vi.fn(),
  prepare: vi.fn(),
  setContext: vi.fn(),
  fetchMe: vi.fn(),
}));

vi.mock("./offlineFiles", () => ({
  beginOfflineFileCacheTransition: protocol.begin,
  finishOfflineFileCacheTransition: protocol.finish,
  isOfflineCacheContext: (value: unknown) =>
    Boolean(value && typeof value === "object"),
  offlineFileCacheContextNeedsTransition: vi.fn(() => false),
  offlineFileCacheFreshnessNeedsTransition: vi.fn(() => false),
  offlineFileCacheTransitionWasLost: vi.fn(() => false),
  prepareOfflineFileCacheContext: protocol.prepare,
  setOfflineFileCacheContext: protocol.setContext,
}));

vi.mock("@/api/endpoints", () => ({ fetchMe: protocol.fetchMe }));

import {
  runOfflineAuthMutation,
  startOidcReauthenticationTransition,
} from "./authTransition";

const authority: MeResponse = {
  id: 11,
  username: "owner",
  display_name: "Owner",
  is_admin: false,
  active: true,
  session_version: 1,
  csrf_token: "csrf",
  offline_cache_generation: "2.cache-authorization",
  auth_method: "oidc",
  locale: "en",
  locales: ["en"],
  vault: {
    id: 101,
    slug: "vault-a",
    name: "Vault A",
    role: "owner",
    can_operate: true,
    delete_enabled: false,
    cloud_deletion_enabled: false,
    is_vault_owner: true,
  },
};

function closedTransition() {
  return {
    id: "transition-a",
    generation: null,
    workerAcknowledged: false,
  };
}

function noWorkerFreshness() {
  return {
    generation: null,
    clientGeneration: 1,
    barrierRevision: "closed",
    barrierState: "closed" as const,
    workerClosed: true,
  };
}

describe("auth transition coordinator", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    protocol.begin.mockResolvedValue(closedTransition());
    protocol.prepare.mockResolvedValue(noWorkerFreshness());
    protocol.fetchMe.mockResolvedValue(authority);
  });

  it("orders close, mutation, fresh authority, and reconciliation in one helper", async () => {
    const events: string[] = [];
    protocol.begin.mockImplementation(async () => {
      events.push("close");
      return closedTransition();
    });
    protocol.prepare.mockImplementation(async () => {
      events.push("worker-probe");
      return noWorkerFreshness();
    });

    const outcome = await runOfflineAuthMutation(
      async () => {
        events.push("mutation");
        return "selected";
      },
      {
        fetchAuthority: async () => {
          events.push("fresh-me");
          return authority;
        },
      },
    );

    expect(events).toEqual(["close", "mutation", "worker-probe", "fresh-me"]);
    expect(outcome.result).toBe("selected");
    expect(outcome.reconciliation?.me.id).toBe(11);
    expect(outcome.reconciliation?.lease).toBeNull();
  });

  it("routes OIDC step-up through the shared close coordinator before redirecting", async () => {
    const events: string[] = [];
    protocol.begin.mockImplementation(async () => {
      events.push("close");
      return closedTransition();
    });

    const transition = await startOidcReauthenticationTransition(() => {
      events.push("redirect");
    });

    expect(events).toEqual(["close", "redirect"]);
    expect(transition.id).toBe("transition-a");
    expect(protocol.prepare).not.toHaveBeenCalled();
    expect(protocol.fetchMe).not.toHaveBeenCalled();
  });
});
