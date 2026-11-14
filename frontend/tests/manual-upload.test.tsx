import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests, type ApiFetch } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { VaultHeader } from "@/layout/VaultHeader";
import { OverviewPage } from "@/pages/overview/OverviewPage";
import { jsonResponse, loadCatalog } from "./vault-settings-harness";

afterEach(resetApiClientForTests);

// #647: Manual Upload is the Owner's choice, never a red state.
const STATS: Record<string, object> = {
  1: { states: { protected: 4 }, protection_state: "protected", manual_upload: true, storage: {} },
  2: { states: { protected: 1, pending: 3 }, protection_state: "pending", manual_upload: true, storage: {} },
  3: { states: { protected: 2 }, protection_state: "protected", manual_upload: false, storage: {} },
};

function setup(ui: React.ReactElement, stats = STATS, paused = false) {
  const messages = loadCatalog("en");
  configureApiClient({ fetch: vi.fn<ApiFetch>(async (input) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    if (url.startsWith("/api/stats")) return jsonResponse(stats[new URL(url, "http://x").searchParams.get("vault_id") ?? ""]);
    if (url.startsWith("/api/general/vaults")) return jsonResponse({ items: Object.keys(stats).map((id) => ({
      id: Number(id), uuid: `v${id}`, name: `Vault ${id}`, paused: paused && id === "1", lifecycle: "active", source_root: `/sources/v${id}`, s3_prefix: `v${id}` })) });
    return jsonResponse({}, 404);
  }) });
  render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider>{ui}</I18nProvider></ApiQueryProvider>);
}

const vaults = [1, 2, 3].map((id) => ({ id, uuid: `v${id}`, name: `Vault ${id}` }));

describe("Manual Upload (#647)", () => {
  it("follows the files: green when all protected, amber with pending files, with a neutral note", async () => {
    setup(<OverviewPage vaults={vaults} />);
    const table = await screen.findByTestId("overview-table");
    const row = async (name: string) => (await within(table).findByText(name)).closest("tr") as HTMLElement;
    const manualProtected = await row("Vault 1");
    expect(await within(manualProtected).findByText("Protected")).toHaveAttribute("data-state", "protected");
    expect(within(manualProtected).getByTestId("manual-upload-note")).toHaveTextContent("Manual upload");
    const manualPending = await row("Vault 2");
    expect(await within(manualPending).findByText("Pending")).toHaveAttribute("data-state", "pending");
    const automatic = await row("Vault 3");
    expect(await within(automatic).findByText("Protected")).toBeInTheDocument();
    expect(within(automatic).queryByTestId("manual-upload-note")).toBeNull();
    expect(screen.queryByText(/suspended/i)).toBeNull();
    expect(table.querySelector('[data-state="problem"]')).toBeNull();
  });

  it("explains pending files of manual Vaults in the pending tile", async () => {
    setup(<OverviewPage vaults={vaults} />);
    expect(await screen.findByTestId("tile-pending-manual")).toHaveTextContent("3 in Vaults with manual upload: upload them with Upload now.");
    expect(screen.getByTestId("tile-pending")).not.toHaveTextContent(loadCatalog("en")["overview.tile_pending_hint"]);
  });

  it("keeps the automatic copy when every Vault uploads automatically", async () => {
    setup(<OverviewPage vaults={[vaults[2]]} />, { 3: { states: { pending: 2 }, protection_state: "pending", manual_upload: false, storage: {} } });
    const tile = await screen.findByTestId("tile-pending");
    await within(tile).findByText("2");
    expect(tile).toHaveTextContent(loadCatalog("en")["overview.tile_pending_hint"]);
    expect(screen.queryByTestId("tile-pending-manual")).toBeNull();
  });

  it("shows only a paused badge in the Overview; the pause action is on the Vault page", async () => {
    setup(<OverviewPage vaults={vaults} />, STATS, true);
    expect(await within(await screen.findByTestId("overview-table")).findByTestId("overview-paused")).toHaveTextContent("Paused Vault");
    expect(screen.queryByRole("button", { name: /cloud work/ })).toBeNull();
  });

  it("puts the pause action in the Vault header of the Files tab", async () => {
    setup(<VaultHeader vaultId={1} vaultName="Vault 1" active="files" t={(key) => loadCatalog("en")[key] ?? key} />);
    const header = await screen.findByTestId("vault-header");
    expect(await within(header).findByRole("button", { name: "Pause cloud work" })).toBeInTheDocument();
    expect(await within(header).findByTestId("manual-upload-note")).toBeInTheDocument();
  });
});
