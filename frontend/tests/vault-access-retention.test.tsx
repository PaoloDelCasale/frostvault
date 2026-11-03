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

describe("VaultAccessPage — per-Vault local release", () => {
  beforeEach(() => resetApiClientForTests());

  function setup(initial: number | null = null) {
    const updates: unknown[] = [];
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/vault/operation-policy": () => jsonResponse({ ...defaultPolicy, local_release_after_days: initial }),
      "PUT /api/vault/operation-policy": (init) => {
        const body = JSON.parse(String(init?.body ?? "{}"));
        updates.push(body);
        return jsonResponse(body);
      },
    }) });
    return updates;
  }

  it("is off by default, preselects six months, and offers only four periods", async () => {
    const user = userEvent.setup();
    const updates = setup();
    await screen.findByText(/local copy retention loaded/i);
    const toggle = screen.getByLabelText(/free local space automatically/i);
    const period = screen.getByRole("combobox", { name: /^free local space after/i });
    expect(toggle).not.toBeChecked();
    expect(period).toBeDisabled();
    expect(period).toHaveValue("180");
    expect(screen.getAllByRole("option").filter((option) => ["90", "180", "365", "730"].includes((option as HTMLOptionElement).value))).toHaveLength(4);
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: /save local copy retention/i }));
    await waitFor(() => expect(updates).toEqual([{ ...defaultPolicy, local_release_after_days: 180 }]));
  });

  it.each([90, 180, 365, 730])("saves %s days without changing upload rules", async (days) => {
    const user = userEvent.setup();
    const updates = setup(180);
    await screen.findByText(/local copy retention loaded/i);
    await user.selectOptions(screen.getByRole("combobox", { name: /^free local space after/i }), String(days));
    await user.click(screen.getByRole("button", { name: /save local copy retention/i }));
    await waitFor(() => expect(updates).toEqual([{ ...defaultPolicy, local_release_after_days: days }]));
  });

  it("disables with null, never a separate boolean or free-form period", async () => {
    const user = userEvent.setup();
    const updates = setup(730);
    await screen.findByText(/local copy retention loaded/i);
    await user.click(screen.getByLabelText(/free local space automatically/i));
    await user.click(screen.getByRole("button", { name: /save local copy retention/i }));
    await waitFor(() => expect(updates).toEqual([{ ...defaultPolicy, local_release_after_days: null }]));
  });
});
