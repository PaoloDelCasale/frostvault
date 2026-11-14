import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { StopProtectionDialog } from "@/components/StopProtectionDialog";
import { formatDateTime } from "@/pages/archive/format";

const messages = JSON.parse(readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app/locales/en.json"), "utf8"));
const initial = { vault_id: 1, vault_name: "Photos", stopped: false, root_released: false,
  cloud_disposition: "keep", group_id: null, jobs: [], cancellable: false, pending_until: null };
const pending = { ...initial, stopped: true, cloud_disposition: "delete", group_id: "group",
  cancellable: true, pending_until: "2026-11-12T12:00:00+00:00", jobs: [{ id: 1, status: "pending_delay", pending_until: "2026-11-12T12:00:00+00:00" }] };

afterEach(() => { cleanup(); resetApiClientForTests(); });

function setup(props: { onOpenChange?: (open: boolean) => void; onCompleted?: () => void } = {}) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  configureApiClient({ fetch: vi.fn(async (input, init) => {
    const url = String(input); const method = init?.method ?? "GET";
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });
    const body = url.startsWith("/api/i18n/catalog") ? { locale: "en", locales: ["en", "it"], messages }
      : url.endsWith("/cancel-deletion") ? { ...initial, stopped: true, root_released: true }
      : method === "POST" ? pending : initial;
    return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  }) });
  render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider><StopProtectionDialog open onOpenChange={props.onOpenChange ?? vi.fn()} onCompleted={props.onCompleted} vaultId={1} vaultName="Photos" /></I18nProvider></ApiQueryProvider>);
  return calls;
}

describe("stop folder protection (#428)", () => {
  it("defaults to keep, never offers local removal, and requires the exact folder name", async () => {
    const user = userEvent.setup(); setup();
    const choice = await screen.findByRole("combobox", { name: "Cloud copy" });
    expect(choice).toHaveValue("keep");
    expect(screen.queryByText(/remove local copies/i)).not.toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Stop protecting this Vault" });
    expect(button).toBeDisabled();
    await user.type(screen.getByLabelText("Reason"), "Stop protection deliberately");
    await user.type(screen.getByLabelText("Type the exact folder name (Photos)"), "photos");
    expect(button).toBeDisabled();
    await user.clear(screen.getByLabelText("Type the exact folder name (Photos)"));
    await user.type(screen.getByLabelText("Type the exact folder name (Photos)"), "Photos");
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("explains seven-day Cloud-only deletion and cancellation keeps cloud without resuming protection", async () => {
    const user = userEvent.setup(); const calls = setup();
    await user.selectOptions(await screen.findByRole("combobox"), "delete");
    expect(screen.getByText(/Only Cloud-only files can be deleted/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Type the exact folder name (Photos)"), "Photos");
    await user.type(screen.getByLabelText("Reason"), "Deliberate deletion");
    await user.click(screen.getByRole("button", { name: "Stop protecting this Vault" }));
    await screen.findByTestId("stop-protection-status");
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ cloud_disposition: "delete", confirmation: "Photos", reason: "Deliberate deletion" });
    expect(screen.getByText(`Deletion cannot begin before ${formatDateTime("2026-11-12T12:00:00+00:00")}.`)).toBeInTheDocument();
    expect(screen.queryByText(/2026-11-12T12/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete now" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel deletion and keep cloud copies" }));
    await screen.findByText("Protection stopped. The local folder is released.");
    expect(calls.some((c) => c.url === "/api/vaults/1/stop-protection/cancel-deletion")).toBe(true);
  });

  it("keeps the outcome with the formatted deadline on screen and leaves only when closed (#652)", async () => {
    const user = userEvent.setup(); const onOpenChange = vi.fn(); const onCompleted = vi.fn();
    setup({ onOpenChange, onCompleted });
    await user.selectOptions(await screen.findByRole("combobox"), "delete");
    await user.type(screen.getByLabelText("Type the exact folder name (Photos)"), "Photos");
    await user.type(screen.getByLabelText("Reason"), "Deliberate deletion");
    await user.click(screen.getByRole("button", { name: "Stop protecting this Vault" }));
    const status = await screen.findByTestId("stop-protection-status");
    expect(status).toHaveTextContent(formatDateTime("2026-11-12T12:00:00+00:00"));
    expect(screen.getByRole("button", { name: "Cancel deletion and keep cloud copies" })).toBeInTheDocument();
    expect(onCompleted).not.toHaveBeenCalled();
    await user.click(screen.getAllByRole("button", { name: "Close" }).at(-1)!);
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(onCompleted).toHaveBeenCalledTimes(1);
  });
});
