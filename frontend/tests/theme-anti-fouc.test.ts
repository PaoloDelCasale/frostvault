import { readFileSync } from "node:fs";
import path from "node:path";
import { runInNewContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

const frontendRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const indexPath = path.join(frontendRoot, "index.html");
const bootstrapPath = path.join(frontendRoot, "public", "theme-bootstrap.js");
const composePath = path.resolve(frontendRoot, "..", "compose.traefik.yaml");

const cssPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../src/index.css",
);
const bootstrapSource = readFileSync(bootstrapPath, "utf8");

interface BootstrapOptions {
  path?: string;
  stored?: Record<string, string>;
  systemDark?: boolean;
  readFailure?: boolean;
  removeFailure?: boolean;
}

function executeProductionBootstrap({
  path: pathname = "/",
  stored = {},
  systemDark = false,
  readFailure = false,
  removeFailure = false,
}: BootstrapOptions = {}) {
  window.history.replaceState({}, "", pathname);
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("style");
  document.documentElement.classList.remove("dark");

  const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation((key) => {
    if (readFailure) throw new DOMException("Storage access denied");
    return stored[key] ?? null;
  });
  const removeItem = vi
    .spyOn(Storage.prototype, "removeItem")
    .mockImplementation(() => {
      if (removeFailure) throw new DOMException("Storage access denied");
    });
  const matchMedia = vi.fn().mockReturnValue({ matches: systemDark });
  vi.stubGlobal("matchMedia", matchMedia);

  runInNewContext(bootstrapSource, { document, encodeURIComponent, window });

  return { getItem, matchMedia, removeItem };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

describe("dark theme first paint", () => {
  it("runs the safe storage/system resolver before the module app script", () => {
    const html = readFileSync(indexPath, "utf8");
    const bootstrap = readFileSync(bootstrapPath, "utf8");
    const resolver = html.indexOf('<script src="/theme-bootstrap.js"></script>');
    const app = html.indexOf('src="/src/main.tsx"');

    expect(resolver).toBeGreaterThanOrEqual(0);
    expect(bootstrap).toContain("window.localStorage");
    expect(bootstrap).toContain("window.matchMedia");
    expect(bootstrap).toContain("root.dataset.theme");
    expect(bootstrap).toContain("root.style.colorScheme");
    expect(app).toBeGreaterThan(resolver);
  });

  it("executes the production bootstrap with the guest preference", () => {
    const { getItem } = executeProductionBootstrap({
      stored: { frostvault_theme_guest: "dark" },
    });

    expect(getItem).toHaveBeenCalledWith("frostvault_theme_active_user");
    expect(getItem).toHaveBeenCalledWith("frostvault_theme_guest");
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(document.documentElement).toHaveClass("dark");
    expect(document.documentElement.style.colorScheme).toBe("dark");
  });

  it("executes the production bootstrap with the active user's preference", () => {
    const { getItem } = executeProductionBootstrap({
      stored: {
        frostvault_theme_active_user: "user / 42",
        frostvault_theme_guest: "dark",
        "frostvault_theme_user_user%20%2F%2042": "light",
      },
      systemDark: true,
    });

    expect(getItem).toHaveBeenCalledWith("frostvault_theme_user_user%20%2F%2042");
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(document.documentElement).not.toHaveClass("dark");
  });

  it("clears a stale identity on login and applies the guest preference", () => {
    const { getItem, removeItem } = executeProductionBootstrap({
      path: "/login",
      stored: {
        frostvault_theme_active_user: "stale-user",
        frostvault_theme_guest: "dark",
        "frostvault_theme_user_stale-user": "light",
      },
    });

    expect(removeItem).toHaveBeenCalledWith("frostvault_theme_active_user");
    expect(getItem).not.toHaveBeenCalledWith("frostvault_theme_active_user");
    expect(getItem).toHaveBeenCalledWith("frostvault_theme_guest");
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });

  it.each([
    [false, "light"],
    [true, "dark"],
  ] as const)("resolves a system preference when system dark is %s", (systemDark, theme) => {
    const { matchMedia } = executeProductionBootstrap({
      stored: { frostvault_theme_guest: "system" },
      systemDark,
    });

    expect(matchMedia).toHaveBeenCalledWith("(prefers-color-scheme: dark)");
    expect(document.documentElement).toHaveAttribute("data-theme", theme);
  });

  it("falls back to the system preference when storage reads fail", () => {
    expect(() =>
      executeProductionBootstrap({ readFailure: true, systemDark: true }),
    ).not.toThrow();
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });

  it("still applies the guest preference on login when stale identity removal fails", () => {
    expect(() =>
      executeProductionBootstrap({
        path: "/login",
        stored: { frostvault_theme_guest: "dark" },
        removeFailure: true,
      }),
    ).not.toThrow();
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });

  it("does not put an inline resolver behind the production self-only script CSP", () => {
    const html = readFileSync(indexPath, "utf8");
    const csp = readFileSync(composePath, "utf8");
    const inlineScripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)]
      .filter(([, attributes]) => !/\bsrc\s*=/i.test(attributes ?? ""));

    expect(csp).toMatch(/script-src 'self'(?:;|")/);
    expect(csp).not.toContain("script-src 'self' 'unsafe-inline'");
    expect(inlineScripts).toHaveLength(0);
    expect(html).toContain('<script src="/theme-bootstrap.js"></script>');
  });

  it("declares explicit semantic dark tokens and native color-scheme", () => {
    const css = readFileSync(cssPath, "utf8");
    expect(css).toMatch(/:root\s*\{[\s\S]*color-scheme:\s*light/);
    expect(css).toContain('[data-theme="dark"]');
    expect(css).toContain("color-scheme: dark");
    expect(css).toContain("--state-both-bg");
    expect(css).toContain("--state-mixed-bg");
    expect(css).toContain("--storage-glacier-border");
    expect(css).toContain("--toast-success-bg");
    expect(css).toContain("--overlay");
  });
});
