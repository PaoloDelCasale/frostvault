import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SourceDirectoryBrowseResponse } from "@/api";
import { SourceDirectoryBrowser } from "@/components/SourceDirectoryBrowser";
import { I18nContext, type I18nContextValue } from "@/i18n/context";
import { translate } from "@/i18n/translate";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);

function loadMessages(): Record<string, string> {
  return JSON.parse(
    readFileSync(path.join(localesDir, "en.json"), "utf8"),
  ) as Record<string, string>;
}

function browseResponse(
  overrides: Partial<SourceDirectoryBrowseResponse> = {},
): SourceDirectoryBrowseResponse {
  return {
    volume_alias: "photos",
    relative_path: "",
    items: [
      {
        name: "family",
        relative_path: "family",
        navigable: true,
        selectable: true,
        occupation: null,
      },
      {
        name: "vacation",
        relative_path: "vacation",
        navigable: false,
        selectable: false,
        occupation: {
          kind: "vault",
          vault_name: "Vacation Archive",
          owner_display_name: "Bob",
        },
      },
    ],
    ...overrides,
  };
}

function renderBrowser(
  ui: React.ReactElement,
  messages: Record<string, string>,
) {
  const value: I18nContextValue = {
    locale: "en",
    locales: ["en", "it"],
    ready: true,
    t: (key, params) => translate(messages, key, params),
    setLocale: vi.fn(async () => undefined),
  };
  return render(
    <I18nContext.Provider value={value}>{ui}</I18nContext.Provider>,
  );
}

describe("SourceDirectoryBrowser", () => {
  afterEach(() => {
    cleanup();
  });

  it("shows admin occupied metadata and stays usable at 375px", async () => {
    const user = userEvent.setup();
    const messages = loadMessages();
    const browse = vi.fn().mockResolvedValue(browseResponse());
    const onSelect = vi.fn();

    renderBrowser(
      <div style={{ width: 375 }}>
        <SourceDirectoryBrowser
          volumeAlias="photos"
          browse={browse}
          selectedPath={null}
          onSelect={onSelect}
          viewerIsAdmin
        />
      </div>,
      messages,
    );

    await waitFor(() => {
      expect(screen.getByText("family")).toBeInTheDocument();
    });
    expect(
      screen.getByText("Occupied by Vacation Archive (owner: Bob)"),
    ).toBeInTheDocument();

    const browser = screen.getByTestId("source-directory-browser");
    expect(browser.getBoundingClientRect().width).toBeLessThanOrEqual(375);

    await user.click(screen.getAllByRole("button", { name: "Select" })[0]);
    expect(onSelect).toHaveBeenCalledWith("family");
  });

  it("shows a generic occupied label for Users", async () => {
    const messages = loadMessages();
    const browse = vi.fn().mockResolvedValue(
      browseResponse({
        items: [
          {
            name: "vacation",
            relative_path: "vacation",
            navigable: false,
            selectable: false,
            occupation: { kind: "vault", label: "Occupied by a Vault" },
          },
        ],
      }),
    );

    renderBrowser(
      <div style={{ width: 375 }}>
        <SourceDirectoryBrowser
          volumeAlias="photos"
          browse={browse}
          selectedPath={null}
          onSelect={vi.fn()}
          viewerIsAdmin={false}
        />
      </div>,
      messages,
    );

    await waitFor(() => {
      expect(screen.getByText("Occupied by a Vault")).toBeInTheDocument();
    });
    expect(screen.queryByText(/Vacation Archive/)).not.toBeInTheDocument();
  });
});
