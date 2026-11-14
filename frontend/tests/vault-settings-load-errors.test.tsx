import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";
import { createVaultAccessFetch, jsonResponse, renderVaultAccess } from "./vault-settings-harness";

afterEach(resetApiClientForTests);

describe("Settings panels show loading errors inline (#656)", () => {
  it("keeps the operation-policy loading error inside the panels, not in a toast", async () => {
    const fetchImpl = createVaultAccessFetch({
      "GET /api/general/vaults": () => jsonResponse({ items: [] }),
      "GET /api/vault/operation-policy": () => jsonResponse({ detail: "Policy unavailable" }, 500),
    });
    renderVaultAccess({ fetchImpl });
    const panel = await screen.findByTestId("vault-settings");
    await waitFor(() => expect(within(panel).getAllByRole("alert").length).toBeGreaterThanOrEqual(3));
    for (const alert of screen.getAllByRole("alert")) {
      expect(panel).toContainElement(alert);
      expect(alert).toHaveTextContent("Policy unavailable");
    }
  });
});
