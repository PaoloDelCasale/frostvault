import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";

import {
  createVaultAccessFetch,
  defaultLifecycle,
  jsonResponse,
  loadCatalog,
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

  it("prefills guided rules, blocks invalid absolute days/depth/minima, warns for cold classes, and saves exact custom rules", async () => {
    const user = userEvent.setup();
    const calls: unknown[] = [];
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/lifecycle": () => jsonResponse(defaultLifecycle),
      "PUT /api/vault/lifecycle/default": (init) => {
        const body = JSON.parse(String(init?.body ?? "{}"));
        calls.push(body);
        return jsonResponse({
          ...defaultLifecycle,
          default_policy_id: "custom-default",
          policies: [
            { id: "custom-default", name: "Vault default", profile: body.profile },
          ],
          warnings: ["cold warning"],
        });
      },
    });

    renderVaultAccess({ fetchImpl: fetchMock });
    await screen.findByText(/lifecycle policy loaded/i);
    await user.selectOptions(
      screen.getByLabelText(/vault default profile/i),
      "archive_tiered",
    );
    await user.click(screen.getAllByRole("button", { name: /customize/i })[0]);

    const dayInputs = screen.getAllByLabelText(/after n days from creation/i);
    expect(dayInputs).toHaveLength(2);
    expect(dayInputs[0]).toHaveValue(30);
    expect(dayInputs[1]).toHaveValue(90);
    expect(screen.getByText(/cold storage can add retrieval charges/i)).toBeInTheDocument();

    await user.clear(dayInputs[1]);
    await user.type(dayInputs[1], "20");
    await user.selectOptions(
      screen.getAllByLabelText(/target storage class/i)[1],
      "ONEZONE_IA",
    );
    await user.click(screen.getByRole("button", { name: /save custom rules/i }));
    expect(screen.getByText(/same-band classes are not allowed/i)).toBeInTheDocument();
    expect(screen.getByText(/onezone_ia requires at least 30 days/i)).toBeInTheDocument();
    expect(calls).toEqual([]);

    await user.clear(dayInputs[1]);
    await user.type(dayInputs[1], "180");
    await user.selectOptions(
      screen.getAllByLabelText(/target storage class/i)[1],
      "DEEP_ARCHIVE",
    );
    await user.click(
      screen.getByLabelText(/add rules for noncurrent archive versions/i),
    );
    await user.type(
      screen.getAllByLabelText(/after n days from creation/i)[2],
      "180",
    );
    await user.selectOptions(
      screen.getAllByLabelText(/target storage class/i)[2],
      "DEEP_ARCHIVE",
    );
    await user.click(screen.getByRole("button", { name: /save custom rules/i }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({
      profile: {
        transitions: [
          { days: 30, storage_class: "STANDARD_IA" },
          { days: 180, storage_class: "DEEP_ARCHIVE" },
        ],
        expiration_days: null,
        noncurrent_expiration_days: null,
        noncurrent_transitions: [
          { days: 180, storage_class: "DEEP_ARCHIVE" },
        ],
      },
    });
    expect(screen.getByLabelText(/vault default profile/i)).toHaveValue("__custom__");
  });

  it("saves a custom folder ladder and renders it in the override list", async () => {
    const user = userEvent.setup();
    let requestBody: Record<string, unknown> | null = null;
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/lifecycle": () => jsonResponse(defaultLifecycle),
      "PUT /api/vault/lifecycle/folder-overrides": (init) => {
        requestBody = JSON.parse(String(init?.body ?? "{}"));
        const profile = (requestBody as { profile: unknown }).profile;
        return jsonResponse({
          ...defaultLifecycle,
          folder_overrides: [{ folder_path: "photos/2024", policy_id: "folder-custom" }],
          policies: [{ id: "folder-custom", name: "Folder photos/2024", profile }],
        });
      },
    });

    renderVaultAccess({ fetchImpl: fetchMock });
    await screen.findByText(/lifecycle policy loaded/i);
    await user.type(screen.getByLabelText(/folder path/i), "photos/2024");
    await user.click(screen.getAllByRole("button", { name: /customize/i })[1]);
    await user.click(screen.getByRole("button", { name: /save custom rules/i }));
    await waitFor(() => expect(requestBody).not.toBeNull());
    expect(requestBody).toMatchObject({
      folder_path: "photos/2024",
      profile: {
        transitions: [
          { days: 30, storage_class: "STANDARD_IA" },
          { days: 90, storage_class: "GLACIER" },
        ],
      },
    });
    expect(await screen.findByText("photos/2024")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /edit rules/i })).toBeInTheDocument();
  });

  it("has complete English and Italian lifecycle editor catalogs", () => {
    for (const locale of ["en", "it"] as const) {
      const catalog = loadCatalog(locale);
      for (const key of [
        "access.lifecycle_customize",
        "access.lifecycle_rule_days",
        "access.lifecycle_error_days_absolute",
        "access.lifecycle_error_depth",
        "access.lifecycle_error_minimum",
        "access.lifecycle_cold_warning",
      ]) {
        expect(catalog[key]).toBeTruthy();
      }
    }
  });
});
