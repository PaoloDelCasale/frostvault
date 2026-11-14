import { afterEach, describe, expect, it } from "vitest";
import { interceptLinkClick, legacyRedirect, navigate, parseRoute, vaultSettingsUrl } from "@/router";

afterEach(() => { window.history.replaceState({}, "", "/"); document.body.innerHTML = ""; });

describe("client routes (#512)", () => {
  it.each([
    ["/", { name: "overview" }],
    ["/vaults/7/files", { name: "files", vaultId: 7 }],
    ["/vaults/7/settings/", { name: "settings", vaultId: 7 }],
    ["/problems", { name: "problems" }],
    ["/system", { name: "system" }],
    ["/vaults/new", { name: "new-vault" }],
    ["/vaults/0/files", { name: "not-found" }],
    ["/vaults/abc/files", { name: "not-found" }],
    ["/general/extra", { name: "not-found" }],
  ])("parses %s", (path, route) => expect(parseRoute(path)).toEqual(route));

  it.each([
    ["/", "?vault_id=3", "/vaults/3/files"],
    ["/", "?vault_id=3&directory=a%2Fb&file=a%2Fb%2Fc.jpg&state=problem", "/vaults/3/files?directory=a%2Fb&file=a%2Fb%2Fc.jpg&state=problem"],
    ["/", "?vault_id=3&settings=1", "/vaults/3/settings"],
    ["/", "?vault_id=nope", "/"],
    ["/general", "", "/system"],
  ])("redirects legacy %s%s", (path, search, target) => expect(legacyRedirect(path, search)).toBe(target));

  it.each([["/", ""], ["/", "?q=x"], ["/vaults/3/files", "?vault_id=3"], ["/system", ""]])("keeps current %s%s", (path, search) => {
    expect(legacyRedirect(path, search)).toBeNull();
  });

  it("normalizes legacy targets when navigating", () => {
    navigate("/?vault_id=4&file=x");
    expect(`${window.location.pathname}${window.location.search}`).toBe("/vaults/4/files?file=x");
  });

  it("keeps a settings section fragment through navigation (#650)", () => {
    expect(vaultSettingsUrl(4, "protection")).toBe("/vaults/4/settings#protection");
    document.body.innerHTML = `<a id="to" href="${vaultSettingsUrl(4, "protection")}">to</a>`;
    document.body.addEventListener("click", interceptLinkClick, { once: true });
    document.getElementById("to")!.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
    expect(`${window.location.pathname}${window.location.hash}`).toBe("/vaults/4/settings#protection");
  });

  it("intercepts plain internal links but leaves server, download and modified clicks alone", () => {
    document.body.innerHTML = '<a id="in" href="/problems">in</a><a id="api" href="/api/x">api</a><a id="dl" href="/system" download>dl</a><a id="out" href="https://example.com/">out</a>';
    const click = (id: string, init: MouseEventInit = {}) => {
      const event = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, ...init });
      document.body.addEventListener("click", interceptLinkClick, { once: true });
      document.getElementById(id)!.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(click("in")).toBe(true);
    expect(window.location.pathname).toBe("/problems");
    expect(click("in", { ctrlKey: true })).toBe(false);
    expect(click("api")).toBe(false);
    expect(click("dl")).toBe(false);
    expect(click("out")).toBe(false);
  });
});
