import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { resetApiClientForTests } from "@/api";

import {
  createVaultAccessFetch,
  defaultPolicy,
  jsonResponse,
  renderVaultAccess,
} from "./vault-access-harness";

describe("VaultAccessPage — operation policy globs (seam 6)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("can suspend automatic uploads without changing the existing rules", async () => {
    const user = userEvent.setup();
    const updates: unknown[] = [];
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/operation-policy": () => jsonResponse({
        ...defaultPolicy, auto_upload: true, local_release_after_days: 180, exclude_globs: ["tmp/**"],
      }),
      "PUT /api/vault/operation-policy": (init) => {
        const body: unknown = JSON.parse(String(init?.body ?? "{}"));
        updates.push(body);
        return jsonResponse(body);
      },
    });
    renderVaultAccess({ fetchImpl: fetchMock });
    const enabled = await screen.findByRole("checkbox", { name: /enable automatic upload for this vault/i });
    await waitFor(() => expect(enabled).toBeChecked());
    expect(screen.getByText(/always excluded from automatic upload/i)).toHaveTextContent("*-journal");
    await user.click(enabled);
    await user.click(screen.getByRole("button", { name: /save operation policy/i }));
    await waitFor(() => expect(updates).toHaveLength(1));
    expect(updates[0]).toMatchObject({ auto_upload: false, exclude_globs: ["tmp/**"] });
    expect(updates[0]).not.toHaveProperty("local_release_after_days");
    expect(enabled).not.toBeChecked();
  });

  it("renders include and exclude rules as chips and previews one path", async () => {
    const user = userEvent.setup();
    const previews: unknown[] = [];
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/operation-policy": () =>
        jsonResponse({
          ...defaultPolicy,
          include_globs: ["**/*.txt"],
          exclude_globs: ["tmp/**"],
        }),
      "POST /api/vault/operation-policy/preview-globs": (init) => {
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          paths: string[];
          include_globs: string[];
          exclude_globs: string[];
        };
        previews.push(body);
        expect(body.include_globs).toEqual(["**/*.txt"]);
        expect(body.paths).toEqual(["docs/a.txt"]);
        if (body.exclude_globs.length) {
          return jsonResponse({
            included: ["docs/a.txt"],
            excluded: [],
          });
        }
        return jsonResponse({ included: ["docs/a.txt"], excluded: [] });
      },
    });

    renderVaultAccess({ fetchImpl: fetchMock });
    await screen.findByText(/operation policy loaded/i);
    expect(within(screen.getByTestId("include-globs-rules")).getByText("**/*.txt")).toBeInTheDocument();
    expect(within(screen.getByTestId("exclude-globs-rules")).getByText("tmp/**")).toBeInTheDocument();

    await user.type(screen.getByLabelText(/vault-relative path/i), "docs/a.txt");
    await user.click(screen.getByRole("button", { name: /^try$/i }));

    const preview = await screen.findByTestId("glob-preview");
    expect(preview).toHaveTextContent("docs/a.txt");
    expect(preview).toHaveTextContent(/would be uploaded automatically/i);
    await waitFor(() => expect(previews).toHaveLength(1));
  });

  it("explains a path skipped by an exclude rule", async () => {
    const user = userEvent.setup();
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/operation-policy": () =>
        jsonResponse({
          ...defaultPolicy,
          include_globs: ["**/*.txt"],
          exclude_globs: ["tmp/**"],
        }),
      "POST /api/vault/operation-policy/preview-globs": (init) => {
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          paths: string[];
          exclude_globs: string[];
        };
        if (body.exclude_globs.length) {
          return jsonResponse({ included: [], excluded: body.paths });
        }
        return jsonResponse({ included: body.paths, excluded: [] });
      },
    });

    renderVaultAccess({ fetchImpl: fetchMock });
    await screen.findByText(/operation policy loaded/i);
    await user.type(screen.getByLabelText(/vault-relative path/i), "tmp/b.txt");
    await user.click(screen.getByRole("button", { name: /^try$/i }));
    expect(await screen.findByTestId("glob-preview")).toHaveTextContent(
      /skipped by an exclude rule/i,
    );
  });
});
