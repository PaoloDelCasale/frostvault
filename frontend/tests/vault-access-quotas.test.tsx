import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";

import {
  createVaultAccessFetch,
  jsonResponse,
  renderVaultAccess,
} from "./vault-access-harness";

describe("VaultAccessPage — quotas (seams 3, 5)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("sends the quota payload the API expects and rejects invalid input client-side", async () => {
    // Ported from tests/test_admin_ui.mjs:
    // "blank quota limits render as unlimited and save as null"
    // "invalid quota order and empty reason do not send or lose the form"
    const user = userEvent.setup();
    const putBodies: unknown[] = [];
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/quotas": () =>
        jsonResponse({
          limits: {
            storage_soft_limit_bytes: null,
            storage_hard_limit_bytes: null,
            concurrency_soft_limit: null,
            concurrency_hard_limit: null,
            restore_30d_soft_limit_bytes: null,
            restore_30d_hard_limit_bytes: null,
          },
          usage: { storage_bytes: 0, concurrency: 0, restore_30d_bytes: 0 },
          evaluation: { state: "evaluated", allowed: true, decisions: [] },
        }),
      "PUT /api/admin/vaults/1/quotas": (init) => {
        const body = JSON.parse(String(init?.body ?? "{}"));
        putBodies.push(body);
        return jsonResponse({
          limits: body,
          usage: { storage_bytes: 0, concurrency: 0, restore_30d_bytes: 0 },
          evaluation: { state: "evaluated", allowed: true, decisions: [] },
        });
      },
    });

    renderVaultAccess({ fetchImpl: fetchMock, isAdmin: true });
    await screen.findByText(/quota status loaded/i);

    const softStorage = screen.getByLabelText(/storage \(soft\)/i);
    const hardStorage = screen.getByLabelText(/storage \(hard\)/i);
    await user.clear(softStorage);
    await user.type(softStorage, "9");
    await user.clear(hardStorage);
    await user.type(hardStorage, "4");
    await user.type(
      screen.getByLabelText(/reason for this quota change/i),
      "bad order",
    );
    await user.click(screen.getByRole("button", { name: /save quotas/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/cannot exceed/i);
    });
    expect(putBodies).toHaveLength(0);

    await user.clear(hardStorage);
    await user.type(hardStorage, "10");
    await user.clear(screen.getByLabelText(/reason for this quota change/i));
    await user.click(screen.getByRole("button", { name: /save quotas/i }));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/reason/i);
    });
    expect(putBodies).toHaveLength(0);

    await user.type(
      screen.getByLabelText(/reason for this quota change/i),
      "remove quota limits",
    );
    await user.clear(softStorage);
    await user.clear(hardStorage);
    await user.click(screen.getByRole("button", { name: /save quotas/i }));

    await waitFor(() => {
      expect(putBodies).toHaveLength(1);
    });
    expect(putBodies[0]).toEqual({
      storage_soft_limit_bytes: null,
      storage_hard_limit_bytes: null,
      concurrency_soft_limit: null,
      concurrency_hard_limit: null,
      restore_30d_soft_limit_bytes: null,
      restore_30d_hard_limit_bytes: null,
      reason: "remove quota limits",
    });
  });

  it("renders block and warning quota states distinguishably by text", async () => {
    // Ported from tests/test_vault_access_ui.mjs:
    // "owner quota state formats the authoritative backend decision"
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/quotas": () =>
        jsonResponse({
          limits: {},
          usage: {},
          evaluation: {
            state: "evaluated",
            allowed: false,
            decisions: [
              {
                code: "quota.storage.hard_exceeded",
                severity: "block",
                projected: 11,
                limit: 10,
              },
              {
                code: "quota.storage.soft_exceeded",
                severity: "warning",
                projected: 11,
                limit: 10,
              },
            ],
          },
        }),
    });

    renderVaultAccess({ fetchImpl: fetchMock });
    const state = await screen.findByTestId("quota-state");
    expect(state).toHaveTextContent(/Block: quota\.storage\.hard_exceeded/);
    expect(state).toHaveTextContent(/Warning: quota\.storage\.soft_exceeded/);
    expect(state).not.toHaveTextContent(
      /No active warnings or blocks reported/,
    );
    expect(within(state).getByText(/Block:/i).closest("[data-quota-state]")).toHaveAttribute(
      "data-quota-state",
      "block",
    );
    expect(
      within(state).getByText(/Warning:/i).closest("[data-quota-state]"),
    ).toHaveAttribute("data-quota-state", "warning");
  });

  it("does not invent an allow result when evaluation is unavailable", async () => {
    // Ported from tests/test_vault_access_ui.mjs:
    // "owner quota state does not invent an allow result when evaluation is unavailable"
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/quotas": () => jsonResponse({ limits: {}, usage: {} }),
    });
    renderVaultAccess({ fetchImpl: fetchMock });
    const state = await screen.findByTestId("quota-state");
    expect(state).toHaveTextContent(/Quota state unavailable/);
    expect(state).not.toHaveTextContent(
      /No active warnings or blocks reported/,
    );
  });

  it("renders the ok quota state by text when there are no decisions", async () => {
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/quotas": () =>
        jsonResponse({
          limits: {},
          usage: {},
          evaluation: { state: "evaluated", allowed: true, decisions: [] },
        }),
    });
    renderVaultAccess({ fetchImpl: fetchMock });
    const state = await screen.findByTestId("quota-state");
    expect(state).toHaveTextContent(/No active warnings or blocks reported/);
    expect(state.querySelector('[data-quota-state="ok"]')).toBeTruthy();
  });
});
