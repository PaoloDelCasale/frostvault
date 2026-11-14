import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests, type GeneralVault } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { ReconnectVaultDialog } from "@/pages/general/ReconnectVaultDialog";
import { VaultSettingsPanel } from "@/pages/vault-settings/VaultSettingsPanel";
import { createVaultAccessFetch, jsonResponse, loadCatalog } from "./vault-settings-harness";

const uuid = "11111111-1111-1111-1111-111111111111";
const root = `/sources/managed/${uuid}`;
type Lifecycle = "active" | "reconnect_required";
function vault(lifecycle: Lifecycle): GeneralVault {
  return { id: 7, uuid, name: "Photos", paused: false, lifecycle, source_root: root, s3_prefix: `vaults/${uuid}/` };
}
function fetcher(lifecycle: Lifecycle, locale: "en" | "it") {
  return createVaultAccessFetch({
    "GET /api/general/vaults": () => jsonResponse({ items: [vault(lifecycle)] }),
    "GET /api/general/source-volumes": () => jsonResponse({ items: [] }),
  }, { locale });
}
afterEach(() => { resetApiClientForTests(); vi.restoreAllMocks(); });

describe("Danger zone folder label follows the Vault lifecycle (#649)", () => {
  it.each([
    ["active", "en"], ["active", "it"], ["reconnect_required", "en"], ["reconnect_required", "it"],
  ] as const)("settings panel for a %s Vault (%s)", async (lifecycle, locale) => {
    const messages = loadCatalog(locale);
    configureApiClient({ fetch: fetcher(lifecycle, locale) });
    render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider>
      <VaultSettingsPanel vaultId={7} vaultName="Photos" onStopped={vi.fn()} />
    </I18nProvider></ApiQueryProvider>);
    const label = lifecycle === "active" ? messages["general.reconnect_current_root"] : messages["general.reconnect_old_root"];
    const other = lifecycle === "active" ? messages["general.reconnect_old_root"] : messages["general.reconnect_current_root"];
    const danger = await screen.findByText(`${label}: ${root}`);
    const section = within(danger.closest("section")!);
    expect(section.queryByText(`${other}: ${root}`)).not.toBeInTheDocument();
    const hint = section.queryByText(messages["general.reconnect_active_hint"]!);
    if (lifecycle === "active") expect(hint).toBeInTheDocument(); else expect(hint).not.toBeInTheDocument();
  });

  it.each([["active", "Current folder"], ["reconnect_required", "Previous folder"]] as const)("reconnect dialog for a %s Vault says «%s»", async (lifecycle, label) => {
    configureApiClient({ fetch: fetcher(lifecycle, "en") });
    render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider>
      <ReconnectVaultDialog vault={vault(lifecycle)} open onOpenChange={() => {}} onNotice={() => {}} onCompleted={async () => {}} />
    </I18nProvider></ApiQueryProvider>);
    expect(await screen.findByText(`${label}: ${root}`)).toBeInTheDocument();
  });

  it("Italian labels match the issue wording", () => {
    const it = loadCatalog("it");
    expect(it["general.reconnect_current_root"]).toBe("Cartella attuale");
    expect(it["general.reconnect_old_root"]).toBe("Cartella precedente");
  });
});
