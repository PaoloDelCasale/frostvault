import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";

import {
  createVaultAccessFetch,
  jsonResponse,
  renderVaultAccess,
} from "./vault-access-harness";

describe("VaultAccessPage — members (seams 1–2)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("renders the lookup result and allows granting a role; unknown user shows a clear message", async () => {
    const user = userEvent.setup();
    const fetchMock = createVaultAccessFetch({
      "POST /api/vault/user-lookup": (init) => {
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          username?: string;
        };
        if (body.username === "ghost") {
          return jsonResponse({ detail: "User not found" }, 404);
        }
        return jsonResponse({
          id: 42,
          username: "alice",
          display_name: "Alice Example",
          current_vault_role: null,
        });
      },
    });

    renderVaultAccess({ fetchImpl: fetchMock });
    await screen.findByRole("heading", { name: /add a member/i });

    await user.type(screen.getByLabelText(/exact username/i), "alice");
    await user.click(screen.getByRole("button", { name: /look up user/i }));

    expect(await screen.findByText("Alice Example")).toBeInTheDocument();
    expect(screen.getByText(/@alice/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /add member/i }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/vault role/i)).toBeInTheDocument();

    await user.clear(screen.getByLabelText(/exact username/i));
    await user.type(screen.getByLabelText(/exact username/i), "ghost");
    await user.click(screen.getByRole("button", { name: /look up user/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/user not found/i);
    });
  });

  it("adding and removing a member calls the right endpoints and refreshes the list", async () => {
    const user = userEvent.setup();
    let members = [
      {
        id: 1,
        username: "owner",
        display_name: "Owner",
        role: "owner",
      },
    ];

    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/members": () => jsonResponse({ items: members }),
      "POST /api/vault/user-lookup": () =>
        jsonResponse({
          id: 42,
          username: "alice",
          display_name: "Alice Example",
          current_vault_role: null,
        }),
      "POST /api/vault/members": (init) => {
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          user_id: number;
          role: string;
        };
        expect(body).toEqual({ user_id: 42, role: "operator" });
        members = [
          ...members,
          {
            id: 42,
            username: "alice",
            display_name: "Alice Example",
            role: body.role,
          },
        ];
        return jsonResponse({ message: "Assignment updated" }, 201);
      },
      "DELETE /api/vault/members/42": () => {
        members = members.filter((item) => item.id !== 42);
        return jsonResponse({ message: "Access removed" });
      },
    });

    renderVaultAccess({ fetchImpl: fetchMock });
    await screen.findByText("Owner");

    await user.type(screen.getByLabelText(/exact username/i), "alice");
    await user.click(screen.getByRole("button", { name: /look up user/i }));
    await screen.findByText("Alice Example");
    await user.selectOptions(screen.getByLabelText(/vault role/i), "operator");
    await user.click(screen.getByRole("button", { name: /add member/i }));

    await waitFor(() => {
      expect(screen.getAllByText("Alice Example").length).toBeGreaterThan(0);
    });
    expect(
      fetchMock.mock.calls.some(
        (call) =>
          String(call[0]) === "/api/vault/members" &&
          (call[1] as RequestInit | undefined)?.method === "POST",
      ),
    ).toBe(true);

    await user.click(screen.getByRole("button", { name: /^remove$/i }));
    await user.click(screen.getByRole("button", { name: /^remove$/i }));

    await waitFor(() => {
      expect(screen.queryByText("@alice")).not.toBeInTheDocument();
    });
    expect(
      fetchMock.mock.calls.some(
        (call) => String(call[0]) === "/api/vault/members/42",
      ),
    ).toBe(true);
  });
});
