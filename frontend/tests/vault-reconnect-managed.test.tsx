import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests, type GeneralVault } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { ReconnectVaultDialog } from "@/pages/general/ReconnectVaultDialog";

const messages = JSON.parse(readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app/locales/en.json"), "utf8"));
const uuid = "11111111-1111-1111-1111-111111111111";
function json(data: unknown, status = 200) { return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } }); }
function vault(sourceRoot: string): GeneralVault {
  return { id: 7, uuid, name: "Photos", lifecycle: "reconnect_required", source_root: sourceRoot, s3_prefix: `vaults/${uuid}/` };
}
function setup(target: GeneralVault) {
  const bodies: unknown[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) return json({ locale: "en", locales: ["en", "it"], messages });
    if (url === "/api/general/source-volumes") {
      return json({ items: [{ alias: "photos", path: "/sources/photos", access: "rw", health: "ok", diagnostic: null, vault_count: 0 }] });
    }
    if (url === "/api/vaults/7/reconnect") {
      bodies.push(JSON.parse(String(init?.body)));
      return json({ vault_id: 7, source_root: target.source_root, reconnect_state: "scan_required", full_scan_required: true });
    }
    throw new Error(`Unexpected request ${url}`);
  });
  configureApiClient({ fetch: fetchMock, csrfToken: "csrf" });
  render(
    <ApiQueryProvider client={createAppQueryClient()}><I18nProvider>
      <ReconnectVaultDialog vault={target} open onOpenChange={() => {}} onNotice={() => {}} onCompleted={async () => {}} />
    </I18nProvider></ApiQueryProvider>,
  );
  return bodies;
}
afterEach(() => { resetApiClientForTests(); vi.restoreAllMocks(); });

describe("Reconnect of a managed Vault (#641)", () => {
  it("offers the Vault's own managed folder and sends its uuid as the folder", async () => {
    const bodies = setup(vault(`/sources/managed/${uuid}`));
    const select = await screen.findByLabelText("Destination Source Volume");
    await waitFor(() => expect(screen.getByRole("option", { name: "photos · ok" })).toBeInTheDocument());
    await userEvent.selectOptions(select, "managed");
    expect(screen.queryByLabelText(messages["general.reconnect_path"])).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Reason for reconnecting"), "restored backup");
    await userEvent.click(screen.getByRole("button", { name: "Accept folder and scan" }));
    await waitFor(() => expect(bodies).toEqual([{ volume_alias: "managed", relative_path: uuid, reason: "restored backup" }]));
  });

  it("never offers managed to a Vault on a custom Source Volume", async () => {
    setup(vault("/sources/photos/album"));
    await screen.findByRole("option", { name: "photos · ok" });
    expect(screen.queryByRole("option", { name: messages["general.reconnect_own_managed"] })).not.toBeInTheDocument();
  });
});
