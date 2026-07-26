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

describe("VaultAccessPage — operation policy globs (seam 6)", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  it("glob preview shows what the globs match before saving", async () => {
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
          include_globs: string[];
          exclude_globs: string[];
        };
        expect(body.include_globs).toEqual(["**/*.txt"]);
        expect(body.exclude_globs).toEqual(["tmp/**"]);
        expect(body.paths).toEqual(["docs/a.txt", "tmp/b.txt", "docs/c.pdf"]);
        return jsonResponse({
          included: ["docs/a.txt"],
          excluded: ["tmp/b.txt", "docs/c.pdf"],
        });
      },
    });

    renderVaultAccess({ fetchImpl: fetchMock });
    await screen.findByText(/operation policy loaded/i);

    await user.type(
      screen.getByLabelText(/sample paths to preview/i),
      "docs/a.txt\ntmp/b.txt\ndocs/c.pdf",
    );
    await user.click(screen.getByRole("button", { name: /preview globs/i }));

    const preview = await screen.findByTestId("glob-preview");
    expect(preview).toHaveTextContent("docs/a.txt");
    expect(preview).toHaveTextContent("tmp/b.txt");
    expect(preview).toHaveTextContent("docs/c.pdf");
    expect(preview.querySelector("h3")).toHaveTextContent(/included/i);

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          (call) =>
            String(call[0]) === "/api/vault/operation-policy/preview-globs",
        ),
      ).toBe(true);
    });
  });
});
