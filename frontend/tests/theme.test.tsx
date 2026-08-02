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
  const { userId, preference, resolvedTheme, setTheme, setUserId } = useTheme();
  return (
    <div>
      <output data-testid="user-id">{userId ?? "guest"}</output>
      <output data-testid="preference">{preference}</output>
      <output data-testid="resolved">{resolvedTheme}</output>
      <button type="button" onClick={() => setUserId(42)}>
        user 42
      </button>
      <button type="button" onClick={() => setUserId(7)}>
        user 7
      </button>
      <button type="button" onClick={() => setUserId(null)}>
        guest
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

  it("switches identities without carrying the previous user's palette", async () => {
    window.localStorage.setItem(themeStorageKey(42), "dark");
    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "user 42" }));
    await waitFor(() => expect(screen.getByTestId("resolved")).toHaveTextContent("dark"));
    fireEvent.click(screen.getByRole("button", { name: "user 7" }));

    await waitFor(() => {
      expect(screen.getByTestId("user-id")).toHaveTextContent("7");
      expect(screen.getByTestId("preference")).toHaveTextContent("system");
      expect(screen.getByTestId("resolved")).toHaveTextContent("light");
    });
    expect(window.localStorage.getItem(THEME_ACTIVE_USER_STORAGE_KEY)).toBe("7");
  });

  it("ignores a stale active-user marker while the login route is loaded", async () => {
    const originalPath = window.location.pathname;
    window.localStorage.setItem(THEME_ACTIVE_USER_STORAGE_KEY, "42");
    window.localStorage.setItem(themeStorageKey(42), "dark");
    window.history.replaceState({}, "", "/login");

    try {
      render(
        <ThemeProvider>
          <ThemeProbe />
        </ThemeProvider>,
      );

      await waitFor(() => {
        expect(screen.getByTestId("user-id")).toHaveTextContent("guest");
        expect(screen.getByTestId("preference")).toHaveTextContent("system");
        expect(screen.getByTestId("resolved")).toHaveTextContent("light");
      });
      expect(window.localStorage.getItem(THEME_ACTIVE_USER_STORAGE_KEY)).toBeNull();
    } finally {
      window.history.replaceState({}, "", originalPath);
    }
  });

  it("falls back to the guest palette when storage reads or removal fail", async () => {
    const getItem = vi.spyOn(window.localStorage, "getItem").mockImplementation(() => {
      throw new Error("storage denied");
    });
    try {
      render(
        <ThemeProvider>
          <ThemeProbe />
        </ThemeProvider>,
      );
      expect(screen.getByTestId("user-id")).toHaveTextContent("guest");
      expect(screen.getByTestId("preference")).toHaveTextContent("system");
    } finally {
      getItem.mockRestore();
    }

    window.localStorage.setItem(THEME_ACTIVE_USER_STORAGE_KEY, "42");
    const removeItem = vi.spyOn(window.localStorage, "removeItem").mockImplementation(() => {
      throw new Error("storage denied");
    });
    try {
      fireEvent.click(screen.getByRole("button", { name: "guest" }));
      await waitFor(() => expect(screen.getByTestId("user-id")).toHaveTextContent("guest"));
      expect(screen.getByTestId("resolved")).toHaveTextContent("light");
    } finally {
      removeItem.mockRestore();
    }
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
