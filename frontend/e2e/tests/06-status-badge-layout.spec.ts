import fs from "node:fs";
import { expect, test } from "@playwright/test";

const messages = JSON.parse(fs.readFileSync(new URL("../../../app/locales/it.json", import.meta.url), "utf8")) as Record<string, string>;
const lastKnownAt = "2026-11-08T22:00:00.000000+00:00";
const longLabel = `${messages["state.unconfirmed"]} · ${messages["state.last_known"].replace("{state}", messages["state.protected"]).replace("{date}", lastKnownAt)}`;

// Exercise the real SPA, catalog, Badge and responsive file-browser layout without
// depending on a filesystem scan or mutating the shared seeded Vaults.
for (const width of [375, 390, 1400]) {
  test(`Italian status labels remain fully readable without horizontal overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    const vault = { id: 1, slug: "family", name: "Family Archive" };
    await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      let json: unknown = {};
      if (pathname === "/api/i18n/catalog") json = { locale: "it", locales: ["en", "it"], messages };
      else if (pathname === "/api/me") json = { id: 1, display_name: "Owner", csrf_token: "test-csrf", auth_method: "local", locale: "it", locales: ["en", "it"], vault };
      else if (pathname === "/api/vaults") json = { items: [vault] };
      else if (pathname === "/api/general") json = { vaults: [], open_problem_count: 0, month: "2026-11", monthly_estimate: 0, previous_month_delta: null, currency: "EUR" };
      else if (pathname === "/api/files") json = { items: [
        { type: "file", name: "unconfirmed.txt", path: "unconfirmed.txt", state: "unconfirmed", last_known_state: "protected", last_known_at: lastKnownAt },
        { type: "file", name: "changed.txt", path: "changed.txt", state: "pending", previous_snapshot_protected: true },
        { type: "file", name: "protected.txt", path: "protected.txt", state: "protected" },
        // Unsupported local entries project to "problem"; a failed source scan
        // overlays "unconfirmed" plus the last evidence, as above.
        { type: "file", name: "symlink", path: "symlink", state: "problem" },
      ], total: 4, aggregate_status: "ready" };
      else if (pathname === "/api/jobs") json = { groups: [] };
      else if (pathname === "/api/notifications") json = { items: [], unread_count: 0 };
      await route.fulfill({ json });
    });
    await page.goto("/?vault_id=1");
    await expect(page.locator("html")).toHaveAttribute("lang", "it");
    const list = page.getByTestId(width < 768 ? "file-list-cards" : "file-list-table");
    const longBadge = list.locator('[data-state="unconfirmed"]');
    await expect(longBadge).toHaveText(longLabel);
    await expect(list.locator('[data-state="pending"]')).toHaveText(`${messages["state.pending"]} · ${messages["state.previous_snapshot_protected"]}`);
    await expect(list.locator('[data-state="problem"]')).toHaveText(messages["state.problem"]);
    await expect(list).toBeVisible();

    const documentSize = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      viewport: document.documentElement.clientWidth,
    }));
    expect(documentSize.scrollWidth, JSON.stringify(documentSize)).toBeLessThanOrEqual(documentSize.viewport);

    for (const badge of await list.locator("[data-state]").all()) {
      const geometry = await badge.evaluate((element) => {
        const box = element.getBoundingClientRect();
        const range = document.createRange();
        range.selectNodeContents(element);
        const text = range.getBoundingClientRect();
        const style = getComputedStyle(element);
        return {
          left: box.left, right: box.right,
          textLeft: text.left, textRight: text.right,
          textTop: text.top, textBottom: text.bottom,
          top: box.top, bottom: box.bottom,
          height: box.height,
          lineHeight: Number.parseFloat(style.lineHeight),
          padding: Number.parseFloat(style.paddingTop) + Number.parseFloat(style.paddingBottom),
        };
      });
      expect(geometry.left).toBeGreaterThanOrEqual(0);
      expect(geometry.right).toBeLessThanOrEqual(width);
      // Text must fit inside the badge, not merely be clipped/ellipsized.
      expect(geometry.textLeft).toBeGreaterThanOrEqual(geometry.left);
      expect(geometry.textRight).toBeLessThanOrEqual(geometry.right);
      expect(geometry.textTop).toBeGreaterThanOrEqual(geometry.top);
      expect(geometry.textBottom).toBeLessThanOrEqual(geometry.bottom);
      if (await badge.getAttribute("data-state") === "protected") {
        expect(geometry.height).toBeCloseTo(geometry.lineHeight + geometry.padding, 0);
      }
      if (width < 768 && await badge.getAttribute("data-state") === "unconfirmed") {
        expect(geometry.height).toBeGreaterThan(geometry.lineHeight + geometry.padding);
      }
    }
  });
}
