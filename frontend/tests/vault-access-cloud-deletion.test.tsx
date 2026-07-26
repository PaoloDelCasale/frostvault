import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";

import {
  createVaultAccessFetch,
  defaultCloudDeletion,
  jsonResponse,
  renderVaultAccess,
} from "./vault-access-harness";

describe("VaultAccessPage — cloud deletion (seam 8)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("cloud deletion toggle persists and reflects the server state after reload", async () => {
    const user = userEvent.setup();
    let enabled = false;
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/cloud-deletion": () =>
        jsonResponse({ ...defaultCloudDeletion, enabled }),
      "PUT /api/vault/cloud-deletion": (init) => {
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          enabled: boolean;
        };
        enabled = body.enabled;
        return jsonResponse({ ...defaultCloudDeletion, enabled });
      },
    });

    renderVaultAccess({ fetchImpl: fetchMock });
    const panel = await screen.findByText(/cloud deletion is disabled \(default\)/i);
    expect(panel).toBeInTheDocument();

    const checkbox = screen.getByLabelText(/enable cloud deletion workflows/i);
    expect(checkbox).not.toBeChecked();
    await user.click(checkbox);
    await user.click(
      screen.getByRole("button", { name: /save cloud deletion setting/i }),
    );

    await screen.findByText(/cloud deletion is enabled for this vault/i);
    expect(screen.getByLabelText(/enable cloud deletion workflows/i)).toBeChecked();
    expect(enabled).toBe(true);

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          (call) =>
            String(call[0]) === "/api/vault/cloud-deletion" &&
            (call[1] as RequestInit | undefined)?.method === "PUT",
        ),
      ).toBe(true);
    });
  });
});
