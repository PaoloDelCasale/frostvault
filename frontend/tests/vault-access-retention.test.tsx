import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";

import {
  createVaultAccessFetch,
  defaultPolicy,
  jsonResponse,
  renderVaultAccess,
} from "./vault-access-harness";

describe("VaultAccessPage — Local Copy retention (seam 4)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("sends the correct PUT payload for Local Copy retention", async () => {
    // Ported from tests/test_vault_access_ui.mjs:
    // "owner can configure automatic Local Copy retention without changing other policy fields"
    const user = userEvent.setup();
    const updates: unknown[] = [];
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/operation-policy": () => jsonResponse(defaultPolicy),
      "PUT /api/vault/operation-policy": (init) => {
        const body = JSON.parse(String(init?.body ?? "{}"));
        updates.push(body);
        return jsonResponse(body);
      },
    });

    renderVaultAccess({ fetchImpl: fetchMock });
    await screen.findByText(/local copy retention loaded/i);

    const days = screen.getByLabelText(/retention after verification/i);
    expect(screen.getByLabelText(/automatically clean verified local copies/i)).toBeChecked();
    expect(days).toHaveValue(45);

    await user.clear(days);
    await user.type(days, "60");
    await user.click(
      screen.getByRole("button", { name: /save local copy retention/i }),
    );

    await waitFor(() => {
      expect(updates).toEqual([
        {
          auto_upload: true,
          auto_local_cleanup: true,
          local_retention_days: 60,
          stability_seconds: 300,
          include_globs: [],
          exclude_globs: [],
          bandwidth_limit_kibps: null,
          operating_windows: [],
        },
      ]);
    });
  });
});
