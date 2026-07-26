import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createLatestRequestScope, resetApiClientForTests } from "@/api";

import {
  createVaultAccessFetch,
  defaultCloudDeletion,
  defaultLifecycle,
  defaultPolicy,
  jsonResponse,
  loadCatalog,
  renderVaultAccess,
} from "./vault-access-harness";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("VaultAccessPage — ownership transfer (seams 9–10)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("only fires after the exact typed confirmation; a wrong string blocks it", async () => {
    const user = userEvent.setup();
    const transferCalls: unknown[] = [];
    const onTransferred = vi.fn();
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/members": () =>
        jsonResponse({
          items: [
            { id: 1, username: "owner", display_name: "Owner", role: "owner" },
            { id: 20, username: "bob", display_name: "Bob", role: "operator" },
          ],
        }),
      "POST /api/vault/transfer-owner": (init) => {
        transferCalls.push(JSON.parse(String(init?.body ?? "{}")));
        return jsonResponse({ message: "Ownership transferred" });
      },
    });

    renderVaultAccess({ fetchImpl: fetchMock, onTransferred });
    await screen.findByText("Bob");
    await user.click(
      screen.getByRole("button", { name: /transfer ownership/i }),
    );
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByLabelText(/confirmation/i), "wrong");
    await user.click(
      within(dialog).getByRole("button", { name: /^transfer ownership$/i }),
    );
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/exact username/i);
    });
    expect(transferCalls).toHaveLength(0);
    expect(onTransferred).not.toHaveBeenCalled();

    await user.clear(within(dialog).getByLabelText(/confirmation/i));
    await user.type(within(dialog).getByLabelText(/confirmation/i), "bob");
    await user.click(
      within(dialog).getByRole("button", { name: /^transfer ownership$/i }),
    );
    await waitFor(() => {
      expect(transferCalls).toEqual([{ new_owner_user_id: 20 }]);
    });
    expect(onTransferred).toHaveBeenCalled();
  });

  it("applies the admin-ui race guards for stale transfer settle", async () => {
    // Ported from tests/test_admin_ui.mjs race cases that apply here.
    const members = createLatestRequestScope();
    const transfers = createLatestRequestScope();

    const pendingMembers = deferred<{ ok: boolean }>();
    const load = members.begin();
    const settleMembers = load.settle(pendingMembers.promise);
    expect(members.hasSettledCurrent()).toBe(false);

    const transfer = deferred<{ ok: boolean }>();
    const transferSettle = transfers.begin().settle(transfer.promise);
    members.begin();
    transfers.begin();
    transfer.resolve({ ok: true });
    expect(await transferSettle).toBeUndefined();

    const errorTransfer = deferred<never>();
    const errorSettle = transfers.begin().settle(errorTransfer.promise);
    transfers.begin();
    errorTransfer.reject(new Error("stale"));
    expect(await errorSettle).toBeUndefined();

    pendingMembers.resolve({ ok: true });
    expect(await settleMembers).toBeUndefined();
  });

  it("blocks transfer while members are still loading after a refresh", async () => {
    const user = userEvent.setup();
    let membersPhase: "initial" | "refresh" = "initial";
    const refreshDeferred = deferred<Response>();

    const wrapped = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.startsWith("/api/i18n/catalog")) {
        return jsonResponse({
          locale: "en",
          locales: ["en", "it"],
          messages: loadCatalog("en"),
        });
      }
      if (url === "/api/vault/members" && method === "GET") {
        if (membersPhase === "initial") {
          return jsonResponse({
            items: [
              {
                id: 1,
                username: "owner",
                display_name: "Owner",
                role: "owner",
              },
              {
                id: 20,
                username: "bob",
                display_name: "Bob",
                role: "operator",
              },
            ],
          });
        }
        return refreshDeferred.promise;
      }
      if (url === "/api/vault/quotas") {
        return jsonResponse({
          limits: {},
          usage: {},
          evaluation: { state: "evaluated", allowed: true, decisions: [] },
        });
      }
      if (url === "/api/vault/operation-policy") {
        return jsonResponse(defaultPolicy);
      }
      if (url === "/api/vault/lifecycle") {
        return jsonResponse(defaultLifecycle);
      }
      if (url === "/api/vault/cloud-deletion") {
        return jsonResponse(defaultCloudDeletion);
      }
      if (url === "/api/vault/transfer-owner") {
        return jsonResponse({ message: "Ownership transferred" });
      }
      throw new Error(`Unexpected ${method} ${url}`);
    });

    renderVaultAccess({ fetchImpl: wrapped, onTransferred: () => undefined });
    await screen.findByText("Bob");

    membersPhase = "refresh";
    await user.click(screen.getByRole("button", { name: /refresh/i }));
    await user.click(
      screen.getByRole("button", { name: /transfer ownership/i }),
    );
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/confirmation/i), "bob");
    await user.click(
      within(dialog).getByRole("button", { name: /^transfer ownership$/i }),
    );

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        /wait for the current vault members/i,
      );
    });

    refreshDeferred.resolve(
      jsonResponse({
        items: [
          { id: 1, username: "owner", display_name: "Owner", role: "owner" },
          { id: 20, username: "bob", display_name: "Bob", role: "operator" },
        ],
      }),
    );
  });
});
