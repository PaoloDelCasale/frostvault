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

  it("names the occupying Vault and stays usable at 375px", async () => {
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
        />
      </div>,
      messages,
    );

    await waitFor(() => {
      expect(screen.getByText("family")).toBeInTheDocument();
    });
    expect(
      screen.getByText("Occupied by Vacation Archive"),
    ).toBeInTheDocument();

    const browser = screen.getByTestId("source-directory-browser");
    expect(browser.getBoundingClientRect().width).toBeLessThanOrEqual(375);

    await user.click(screen.getByRole("button", { name: "Select family" }));
    expect(onSelect).toHaveBeenCalledWith("family");
  });

  it("names each folder in its select button and keeps a 44px touch target (#656)", async () => {
    const messages = loadMessages();
    renderBrowser(
      <SourceDirectoryBrowser
        volumeAlias="photos"
        browse={vi.fn().mockResolvedValue(browseResponse())}
        selectedPath="family"
        onSelect={vi.fn()}
      />,
      messages,
    );
    const selected = await screen.findByRole("button", { name: "Selected: family" });
    expect(selected).toHaveAttribute("aria-pressed", "true");
    const occupied = screen.getByRole("button", { name: "Select vacation" });
    expect(occupied).toBeDisabled();
    for (const button of [selected, occupied]) {
      expect(button).toHaveClass("min-h-11", "min-w-11");
    }
    expect(screen.getByRole("button", { name: messages["ui.source_directory_browser_select_volume_root"] })).toHaveClass("min-h-11");
  });
});
