import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";

import {
  createVaultAccessFetch,
  defaultLifecycle,
  jsonResponse,
  renderVaultAccess,
} from "./vault-access-harness";

describe("VaultAccessPage — lifecycle (seam 7)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("setting the default and adding, editing and deleting a folder override call the right endpoints", async () => {
    const user = userEvent.setup();
    let state = {
      ...defaultLifecycle,
      folder_overrides: [] as { folder_path: string; policy_id: number | string }[],
      policies: [
        { id: 1, name: "ia_after_30", profile: { transitions: [{ days: 30, storage_class: "STANDARD_IA" }] } },
        { id: 2, name: "archive_tiered", profile: { transitions: [{ days: 30, storage_class: "STANDARD_IA" }, { days: 90, storage_class: "GLACIER" }] } },
      ],
      default_policy_id: null as number | null,
    };
    const calls: { method: string; url: string; body?: unknown }[] = [];

    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/lifecycle": () => jsonResponse(state),
      "PUT /api/vault/lifecycle/default": (init) => {
        const body = JSON.parse(String(init?.body ?? "{}"));
        calls.push({ method: "PUT", url: "/api/vault/lifecycle/default", body });
        state = {
          ...state,
          default_policy_id: 1,
          policies: state.policies,
        };
        return jsonResponse(state);
      },
      "PUT /api/vault/lifecycle/folder-overrides": (init) => {
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          folder_path: string;
          guided_profile: string;
        };
        calls.push({
          method: "PUT",
          url: "/api/vault/lifecycle/folder-overrides",
          body,
        });
        state = {
          ...state,
          folder_overrides: [
            ...state.folder_overrides.filter(
              (item) => item.folder_path !== body.folder_path,
            ),
            { folder_path: body.folder_path, policy_id: 2 },
          ],
        };
        return jsonResponse(state);
      },
      "DELETE /api/vault/lifecycle/folder-overrides": (init) => {
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          folder_path: string;
        };
        calls.push({
          method: "DELETE",
          url: "/api/vault/lifecycle/folder-overrides",
          body,
        });
        state = {
          ...state,
          folder_overrides: state.folder_overrides.filter(
            (item) => item.folder_path !== body.folder_path,
          ),
        };
        return jsonResponse(state);
      },
    });

    renderVaultAccess({ fetchImpl: fetchMock });
    await screen.findByText(/lifecycle policy loaded/i);

    await user.selectOptions(
      screen.getByLabelText(/vault default profile/i),
      "ia_after_30",
    );
    await user.click(
      screen.getByRole("button", { name: /save default profile/i }),
    );
    await waitFor(() => {
      expect(calls).toContainEqual({
        method: "PUT",
        url: "/api/vault/lifecycle/default",
        body: { guided_profile: "ia_after_30" },
      });
    });

    await user.type(screen.getByLabelText(/folder path/i), "photos/2024");
    await user.selectOptions(
      screen.getByLabelText(/^profile$/i),
      "archive_tiered",
    );
    await user.click(
      screen.getByRole("button", { name: /add or update override/i }),
    );
    await waitFor(() => {
      expect(screen.getByText("photos/2024")).toBeInTheDocument();
    });
    expect(calls).toContainEqual({
      method: "PUT",
      url: "/api/vault/lifecycle/folder-overrides",
      body: { folder_path: "photos/2024", guided_profile: "archive_tiered" },
    });

    // Edit same path with a different profile (upsert)
    await user.type(screen.getByLabelText(/folder path/i), "photos/2024");
    await user.selectOptions(
      screen.getByLabelText(/^profile$/i),
      "ia_after_30",
    );
    await user.click(
      screen.getByRole("button", { name: /add or update override/i }),
    );
    await waitFor(() => {
      expect(
        calls.filter(
          (call) => call.url === "/api/vault/lifecycle/folder-overrides" && call.method === "PUT",
        ).length,
      ).toBe(2);
    });

    await user.click(screen.getByRole("button", { name: /^remove$/i }));
    await waitFor(() => {
      expect(screen.queryByText("photos/2024")).not.toBeInTheDocument();
    });
    expect(calls).toContainEqual({
      method: "DELETE",
      url: "/api/vault/lifecycle/folder-overrides",
      body: { folder_path: "photos/2024" },
    });
  });
});
