import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import { FileDetails } from "@/pages/archive/FileDetails";
import { jsonResponse } from "./vault-settings-harness";

const t = (key: string) => key;
const stale = { type: "file" as const, name: "same.jpg", path: "same.jpg", state: "cloud_only" };
function setup(state: string, local: boolean, conflict = false) {
  const calls: string[] = [];
  configureApiClient({ fetch: vi.fn(async (input) => {
    const url = String(input); calls.push(url);
    if (url.startsWith("/api/files/detail")) return jsonResponse({ ...stale, state, local_exists: local, cloud_exists: true, cloud_only_conflict: conflict });
    if (url.startsWith("/api/files/versions")) return jsonResponse({ items: [{ id: "v", recoverable: true, storage_class: "STANDARD", provider_version_id: "exact", verified_at: "2026-11-05", integrity: "verified" }] });
    if (url.startsWith("/api/jobs")) return jsonResponse({ groups: [] });
    if (url.startsWith("/api/cloud-deletion/preview")) return jsonResponse({ object_count: 1, version_count: 1, delete_marker_count: 0, byte_count: 12 });
    if (url.startsWith("/api/cloud-deletion")) return jsonResponse({ purge_delay_seconds: 604800 });
    return jsonResponse({});
  }) });
  render(<ApiQueryProvider client={createAppQueryClient()}><FileDetails target={{ item: stale, vaultId: 2, vaultName: "Second" }} t={t} onClose={vi.fn()} onRecover={vi.fn()} onRefresh={vi.fn()} /></ApiQueryProvider>);
  return calls;
}
afterEach(resetApiClientForTests);
describe("deliberate file Details", () => {
  it("revalidates stale Cloud-only selection and never offers cloud deletion for a present local copy", async () => {
    const calls = setup("protected", true);
    await screen.findByText("exact");
    expect(screen.queryByText("essential.danger_zone")).not.toBeInTheDocument();
    expect(calls.every((url) => url.endsWith("vault_id=2"))).toBe(true);
    expect(calls.some((url) => url.includes("purge"))).toBe(false);
  });
  it("holds a conflict and explains external rename or explicit replacement without list deletion", async () => {
    setup("problem", true, true);
    await screen.findByText("essential.conflict_help");
    expect(screen.getByRole("button", { name: "essential.replace_cloud" })).toBeInTheDocument();
    expect(screen.queryByText("essential.danger_zone")).not.toBeInTheDocument();
  });
  it("cloud deletion starts only inside deliberate Details, with a scope-pinned preview and typed confirmation", async () => {
    const calls = setup("cloud_only", false);
    await screen.findByText("exact");
    const user = userEvent.setup(); await user.click(screen.getByText("essential.danger_zone"));
    await user.click(screen.getByRole("button", { name: "ui.cloud_purge_title" }));
    await screen.findByTestId("cloud-purge-form");
    expect(screen.getByTestId("cloud-purge-submit")).toBeDisabled();
    expect(calls).toContain("/api/cloud-deletion/preview?vault_id=2");
    await user.type(screen.getByTestId("cloud-purge-reason"), "Deliberate removal");
    await user.type(screen.getByTestId("cloud-purge-confirmation"), "Second");
    await user.click(screen.getByTestId("cloud-purge-submit"));
    await waitFor(() => expect(calls).toContain("/api/cloud-purge?vault_id=2"));
  });
});
