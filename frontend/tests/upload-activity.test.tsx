import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import type { UploadActivityResponse, UploadActivitySummary } from "@/api/types";
import { UploadActivity } from "@/components/UploadActivity";
import { formatBytes } from "@/pages/archive/format";
import { jsonResponse, loadCatalog } from "./vault-settings-harness";
import { translate } from "@/i18n/translate";

const empty = (): UploadActivitySummary => ({
  stability_wait: { files: 0, bytes: 0, transferred_bytes: 0 },
  queued: { files: 0, bytes: 0, transferred_bytes: 0 },
  uploading: { files: 0, bytes: 0, transferred_bytes: 0 },
  verifying: { files: 0, bytes: 0, transferred_bytes: 0 },
  awaiting_admission_files: 0, next_stable_at: null,
});
const messages = loadCatalog();
const t = (key: string, params?: Record<string, string | number>) => translate(messages, key, params);

function show(activity: UploadActivitySummary, vaultId?: number) {
  const data: UploadActivityResponse = {
    observed_at: "2026-11-06T10:00:00Z", global: activity,
    vaults: [{ ...activity, vault_id: 2, vault_name: "Other Vault <img>" }],
  };
  const fetch = vi.fn<typeof globalThis.fetch>(async () => jsonResponse(data));
  configureApiClient({ fetch });
  const client = createAppQueryClient();
  render(<ApiQueryProvider client={client}><UploadActivity t={t} vaultId={vaultId} /></ApiQueryProvider>);
  return { data, fetch, client };
}
afterEach(() => { cleanup(); vi.useRealTimers(); resetApiClientForTests(); });

describe("aggregate upload activity", () => {
  it.each(["stability_wait", "queued", "uploading", "verifying"] as const)("shows %s globally and by Vault without enumerating Jobs", async (stage) => {
    const activity = empty();
    activity[stage] = { files: 1789, bytes: 102400, transferred_bytes: 51200 };
    if (stage === "stability_wait") activity.next_stable_at = "2026-11-06T10:05:00Z";
    const { fetch } = show(activity);
    const label = t(`activity.${stage}`, { count: 1789 });
    expect(await screen.findAllByText(label)).toHaveLength(2);
    await userEvent.setup().click(screen.getByText(t("activity.by_vault")));
    expect(screen.getByRole("link", { name: "Other Vault <img>" })).toHaveAttribute("href", "/?vault_id=2");
    expect(document.querySelector("img")).toBeNull();
    expect(fetch).toHaveBeenCalledOnce();
    expect(fetch.mock.calls[0]?.[0]).toBe("/api/upload-activity");
    if (stage === "uploading") expect(screen.getAllByText(t("activity.sent", { sent: formatBytes(51200), total: formatBytes(102400) }))).toHaveLength(2);
    if (stage === "stability_wait") expect(screen.getAllByText(t("activity.stable_in", { minutes: 5 }))).toHaveLength(2);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument(); // no fake batch percentage
  });

  it("shows the selected Vault's own activity without expanding anything", async () => {
    const activity = empty(); activity.uploading = { files: 13, bytes: 130, transferred_bytes: 12 };
    show(activity, 2);
    const selected = await screen.findByTestId("selected-vault-activity");
    expect(selected).toBeVisible();
    expect(within(selected).getByText("13 files uploading")).toBeVisible();
    expect(screen.queryByText(t("activity.by_vault"))).not.toBeInTheDocument();
  });
  it("discloses stable files awaiting admission rather than promising transfer start", async () => {
    const activity = empty(); activity.queued.files = 7; activity.awaiting_admission_files = 7;
    show(activity);
    expect(await screen.findAllByText(t("activity.admission", { count: 7 }))).toHaveLength(2);
    expect(screen.queryByText(/stable in about/)).not.toBeInTheDocument();
  });

  it("does not fabricate timing when the observed deadline is unavailable or past", async () => {
    const activity = empty(); activity.stability_wait.files = 4; activity.next_stable_at = "2026-11-06T09:00:00Z";
    show(activity);
    expect(await screen.findAllByText(t("activity.waiting_observation"))).toHaveLength(2);
    expect(screen.queryByText(/in about .*min/)).not.toBeInTheDocument();
  });

  it("is quiet when idle and surfaces unavailable/stale observations", async () => {
    const { fetch, client } = show(empty());
    expect(await screen.findByText(t("activity.idle"))).toBeInTheDocument();
    fetch.mockRejectedValueOnce(new Error("unavailable"));
    await act(async () => { await client.invalidateQueries({ queryKey: ["upload-activity"] }); });
    expect(await screen.findByRole("status")).toHaveTextContent(t("activity.unavailable"));
    expect(screen.getByText(t("activity.idle"))).toBeInTheDocument();
  });

  it("polls bounded installation aggregates even if no selected Vault emits events", async () => {
    const activity = empty(); activity.uploading = { files: 3, bytes: 30, transferred_bytes: 1 };
    vi.useFakeTimers();
    const { fetch, data } = show(activity);
    for (let tick = 0; tick < 3; tick++) await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(screen.getAllByText(t("activity.uploading", { count: 3 }))).toHaveLength(2);
    data.global.uploading.transferred_bytes = 20;
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(fetch).toHaveBeenCalledTimes(2);
    const section = screen.getByTestId("upload-activity");
    expect(within(section).getAllByText(t("activity.sent", { sent: formatBytes(20), total: formatBytes(30) }))).toHaveLength(2);
  });
});
