import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeControl } from "@/components/ThemeControl";
import { ThemeProvider } from "@/theme";
import {
  THEME_ACTIVE_USER_STORAGE_KEY,
  resolveTheme,
  themeStorageKey,
} from "@/theme/theme";
import { useTheme } from "@/theme/useTheme";

function ThemeProbe() {
  const { preference, resolvedTheme, setTheme, setUserId } = useTheme();
  return (
    <div>
      <output data-testid="preference">{preference}</output>
      <output data-testid="resolved">{resolvedTheme}</output>
      <button type="button" onClick={() => setUserId(42)}>
        user 42
      </button>
      <button type="button" onClick={() => setTheme("dark")}>
        dark
      </button>
      <button type="button" onClick={() => setTheme("system")}>
        system
      </button>
    </div>
  );
}

describe("theme preferences", () => {
  const originalMatchMedia = window.matchMedia;

  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
    document.documentElement.classList.remove("dark");
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
    })));
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.stubGlobal("matchMedia", originalMatchMedia);
  });

  it("resolves system preference without changing the green identity", () => {
    expect(resolveTheme("system", "dark")).toBe("dark");
    expect(resolveTheme("system", "light")).toBe("light");
    expect(resolveTheme("dark", "light")).toBe("dark");
  });

  it("responds to system theme changes until a user chooses an override", async () => {
    let onMediaChange: ((event: MediaQueryListEvent) => void) | undefined;
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: false,
      addEventListener: (_event: string, listener: (event: MediaQueryListEvent) => void) => {
        onMediaChange = listener;
      },
      removeEventListener: vi.fn(),
    })));
    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    expect(screen.getByTestId("resolved")).toHaveTextContent("light");
    onMediaChange?.({ matches: true } as MediaQueryListEvent);
    await waitFor(() => expect(screen.getByTestId("resolved")).toHaveTextContent("dark"));

    fireEvent.click(screen.getByRole("button", { name: "dark" }));
    onMediaChange?.({ matches: false } as MediaQueryListEvent);
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
  });

  it("persists an override under the authenticated user's namespace", async () => {
    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "user 42" }));
    fireEvent.click(screen.getByRole("button", { name: "dark" }));

    await waitFor(() => expect(screen.getByTestId("resolved")).toHaveTextContent("dark"));
    expect(window.localStorage.getItem(themeStorageKey(42))).toBe("dark");
    expect(window.localStorage.getItem(THEME_ACTIVE_USER_STORAGE_KEY)).toBe("42");
    expect(window.localStorage.getItem(themeStorageKey(null))).toBeNull();
  });

  it("continues in memory when storage is unavailable", async () => {
    const setItem = vi.spyOn(window.localStorage, "setItem").mockImplementation(() => {
      throw new Error("storage denied");
    });
    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "dark" }));
    await waitFor(() => expect(screen.getByTestId("resolved")).toHaveTextContent("dark"));
    expect(document.documentElement.dataset.theme).toBe("dark");
    setItem.mockRestore();
  });

  it("provides a translated, labelled, 44px appearance control", () => {
    render(
      <ThemeProvider>
        <ThemeControl />
      </ThemeProvider>,
    );

    const select = screen.getByRole("combobox", { name: "Appearance" });
    expect(select).toHaveValue("system");
    expect(select).toHaveClass("min-h-11");
    expect(screen.getByRole("option", { name: "Dark" })).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "dark" } });
    expect(select).toHaveValue("dark");
  });
});
