import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { configureApiClient, resetApiClientForTests } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { StopProtectionDialog } from "@/components/StopProtectionDialog";
import { formatDateTime } from "@/lib/format";
import { jsonResponse } from "./support/http";
import { renderWithQueryClient } from "./support/queryClient";

const messages = JSON.parse(readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app/locales/en.json"), "utf8"));
const initial = { vault_id: 1, vault_name: "Photos", stopped: false, root_released: false,
  cloud_disposition: "keep", group_id: null, jobs: [], cancellable: false, pending_until: null,
  local_files_blocking_cloud_delete: 0 };
const pending = { ...initial, stopped: true, cloud_disposition: "delete", group_id: "group",
  cancellable: true, pending_until: "2026-11-12T12:00:00+00:00", jobs: [{ id: 1, status: "pending_delay", pending_until: "2026-11-12T12:00:00+00:00" }] };

afterEach(() => { cleanup(); resetApiClientForTests(); });

function setup(props: { onOpenChange?: (open: boolean) => void; onCompleted?: () => void; status?: typeof initial } = {}) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  configureApiClient({ fetch: vi.fn(async (input, init) => {
    const url = String(input); const method = init?.method ?? "GET";
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });
    const body = url.startsWith("/api/i18n/catalog") ? { locale: "en", locales: ["en", "it"], messages }
      : url.endsWith("/cancel-deletion") ? { ...initial, stopped: true, root_released: true }
      : method === "POST" ? pending : props.status ?? initial;
    return jsonResponse(body);
  }) });
  renderWithQueryClient(<I18nProvider><StopProtectionDialog open onOpenChange={props.onOpenChange ?? vi.fn()} onCompleted={props.onCompleted} vaultId={1} vaultName="Photos" /></I18nProvider>);
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

  it("disables cloud deletion before confirmation while files are still present on the server (#715)", async () => {
    const user = userEvent.setup(); const calls = setup({ status: { ...initial, local_files_blocking_cloud_delete: 3 } });
    const notice = await screen.findByTestId("stop-delete-unavailable");
    expect(notice).toHaveTextContent("Deleting from the cloud is unavailable: 3 files are still present on the server.");
    expect(notice).toHaveTextContent("only Cloud-only files can be deleted from the cloud");
    const choice = screen.getByRole("combobox", { name: "Cloud copy" });
    expect(screen.getByRole("option", { name: "Delete from cloud after seven days" })).toBeDisabled();
    await user.selectOptions(choice, "delete");
    expect(choice).toHaveValue("keep");
    await user.type(screen.getByLabelText("Type the exact folder name (Photos)"), "Photos");
    await user.type(screen.getByLabelText("Reason"), "Folder retired");
    await user.click(screen.getByRole("button", { name: "Stop protecting this Vault" }));
    await screen.findByTestId("stop-protection-status");
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ cloud_disposition: "keep", confirmation: "Photos", reason: "Folder retired" });
  });

  it("offers cloud deletion when every file is Cloud-only", async () => {
    setup();
    await screen.findByRole("combobox", { name: "Cloud copy" });
    await waitFor(() => expect(screen.getByRole("option", { name: "Delete from cloud after seven days" })).toBeEnabled());
    expect(screen.queryByTestId("stop-delete-unavailable")).not.toBeInTheDocument();
  });
});
