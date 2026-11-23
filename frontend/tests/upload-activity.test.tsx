import { act, cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { configureApiClient, resetApiClientForTests, type UploadActivityResponse, type UploadActivitySummary, type UploadThroughput } from "@/api";
import { UploadActivity } from "@/components/UploadActivity";
import { formatBytes } from "@/lib/format";
import { expectedFinishShort, expectedFinishText, stoppedShort, stoppedText } from "@/lib/throughput";
import { loadCatalog } from "./vault-settings-harness";
import { translate } from "@/i18n/translate";
import { jsonResponse } from "./support/http";
import { createTestQueryClient, renderWithQueryClient } from "./support/queryClient";

const empty = (): UploadActivitySummary => ({
  stability_wait: { files: 0, bytes: 0, transferred_bytes: 0 },
  queued: { files: 0, bytes: 0, transferred_bytes: 0 },
  uploading: { files: 0, bytes: 0, transferred_bytes: 0 },
  verifying: { files: 0, bytes: 0, transferred_bytes: 0 },
  awaiting_admission_files: 0, next_stable_at: null,
  throughput: measured(), expected_finish: null, stopped_since: null,
});
/** Speeds as the sampler reports them (#755); none measured by default. */
function measured(upload: Partial<UploadThroughput["upload"]> = {}, history: UploadThroughput["history"] = []): UploadThroughput {
  return {
    sample_seconds: 2, instant_seconds: 20, average_seconds: 300, history_seconds: 900,
    upload: { bytes_per_second: null, files_per_second: null, average_bytes_per_second: null, ...upload },
    verification: { bytes_per_second: null, average_bytes_per_second: null }, history,
  };
}
const messages = loadCatalog();
const t = (key: string, params?: Record<string, string | number>) => translate(messages, key, params);

function show(activity: UploadActivitySummary, vaultId?: number, extra: UploadActivityResponse["vaults"] = [],
  serverPriority?: UploadActivityResponse["server_priority"]) {
  const data: UploadActivityResponse = {
    observed_at: "2026-11-06T10:00:00Z", global: activity,
    vaults: [{ ...activity, vault_id: 2, vault_name: "Other Vault <img>", paused: false }, ...extra],
    ...(serverPriority ? { server_priority: serverPriority } : {}),
  };
  const fetch = vi.fn<typeof globalThis.fetch>(async () => jsonResponse(data));
  configureApiClient({ fetch });
  const client = createTestQueryClient();
  renderWithQueryClient(<UploadActivity t={t} vaultId={vaultId} />, { client });
  return { data, fetch, client };
}
/** The indicator only exists while work is observed; its panel holds the detail. */
async function openPanel() {
  await userEvent.setup().click(await screen.findByTestId("upload-indicator"));
  return screen.findByTestId("upload-activity");
}
afterEach(() => { cleanup(); vi.useRealTimers(); resetApiClientForTests(); });

describe("aggregate upload activity", () => {
  it.each(["stability_wait", "queued", "uploading", "verifying"] as const)("shows %s globally and by Vault without enumerating Jobs", async (stage) => {
    const activity = empty();
    activity[stage] = { files: 1789, bytes: 102400, transferred_bytes: 51200 };
    if (stage === "stability_wait") activity.next_stable_at = "2026-11-06T10:05:00Z";
    const { fetch } = show(activity);
    expect(await screen.findByTestId("upload-indicator")).toHaveAccessibleName(t("activity.indicator", { count: 1789, size: formatBytes(102400) }));
    await openPanel();
    const label = t(`activity.${stage}`, { count: 1789 });
    expect(await screen.findAllByText(label)).toHaveLength(2);
    await userEvent.setup().click(screen.getByText(t("activity.by_vault")));
    expect(screen.getByRole("link", { name: "Other Vault <img>" })).toHaveAttribute("href", "/vaults/2/files");
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
    await openPanel();
    const selected = await screen.findByTestId("selected-vault-activity");
    expect(selected).toBeVisible();
    expect(within(selected).getByText("13 files uploading")).toBeVisible();
    expect(screen.queryByText(t("activity.by_vault"))).not.toBeInTheDocument();
  });
  it("cancels every pending upload of the selected Vault in one confirmed request", async () => {
    const activity = empty(); activity.queued = { files: 4292, bytes: 4292, transferred_bytes: 0 };
    const { fetch } = show(activity, 2);
    fetch.mockImplementation(async (input) => /^\/api\/vaults\/\d+\/upload\/cancel-pending/.test(String(input))
      ? jsonResponse({ message: "x", cancelled_count: 4292 })
      : jsonResponse({ observed_at: "2026-11-06T10:00:00Z", global: activity, vaults: [{ ...activity, vault_id: 2, vault_name: "V", paused: false }] }));
    await openPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: t("activity.cancel_pending") }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: t("activity.cancel_pending") }));
    expect(await screen.findByText(t("activity.cancel_pending_done", { count: 4292 }))).toBeVisible();
    const call = fetch.mock.calls.find(([url]) => /^\/api\/vaults\/\d+\/upload\/cancel-pending/.test(String(url)));
    expect(call?.[0]).toBe("/api/vaults/2/upload/cancel-pending");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ path: "" });
  });

  it("shows separate admission progress and cancellation before the first transfer Job", async () => {
    const activity = empty();
    activity.bulk_admission = { requests: 1, selected_files: 26236, processed_files: 200, admitted_files: 100 };
    show(activity, 2);
    expect(await screen.findByTestId("upload-indicator")).toHaveAccessibleName(t("activity.preparing"));
    await openPanel();
    const selected = await screen.findByTestId("selected-vault-activity");
    expect(within(selected).getByText(t("activity.bulk_admission", { selected: 26236, processed: 200, admitted: 100 }))).toBeVisible();
    expect(within(selected).getByRole("button", { name: t("activity.cancel_pending") })).toBeVisible();
    expect(screen.queryByText(/files uploading/)).not.toBeInTheDocument();
  });

  it("offers no cancel action when no upload is waiting", async () => {
    const activity = empty(); activity.uploading = { files: 3, bytes: 30, transferred_bytes: 10 };
    show(activity, 2);
    await openPanel();
    await screen.findByTestId("selected-vault-activity");
    expect(screen.queryByRole("button", { name: t("activity.cancel_pending") })).not.toBeInTheDocument();
  });

  it("discloses stable files awaiting admission rather than promising transfer start", async () => {
    const activity = empty(); activity.queued.files = 7; activity.awaiting_admission_files = 7;
    show(activity);
    await openPanel();
    expect(await screen.findAllByText(t("activity.admission", { count: 7 }))).toHaveLength(2);
    expect(screen.queryByText(/stable in about/)).not.toBeInTheDocument();
  });

  it("does not fabricate timing when the observed deadline is unavailable or past", async () => {
    const activity = empty(); activity.stability_wait.files = 4; activity.next_stable_at = "2026-11-06T09:00:00Z";
    show(activity);
    await openPanel();
    expect(await screen.findAllByText(t("activity.waiting_observation"))).toHaveLength(2);
    expect(screen.queryByText(/in about .*min/)).not.toBeInTheDocument();
  });

  it("shows nothing while idle", async () => {
    const { fetch } = show(empty());
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByTestId("upload-indicator")).not.toBeInTheDocument();
  });

  it("keeps the last observation and says when it could not be refreshed", async () => {
    const activity = empty(); activity.uploading = { files: 2, bytes: 20, transferred_bytes: 1 };
    const { fetch, client } = show(activity);
    await openPanel();
    fetch.mockRejectedValueOnce(new Error("unavailable"));
    await act(async () => { await client.invalidateQueries({ queryKey: ["upload-activity"] }); });
    expect(await screen.findByRole("status")).toHaveTextContent(t("activity.unavailable"));
    expect(screen.getAllByText(t("activity.uploading", { count: 2 }))[0]).toBeInTheDocument();
  });

  it("polls bounded installation aggregates even if no selected Vault emits events", async () => {
    const activity = empty(); activity.uploading = { files: 3, bytes: 30, transferred_bytes: 1 };
    vi.useFakeTimers();
    const { fetch, data } = show(activity);
    for (let tick = 0; tick < 3; tick++) await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    await act(async () => { screen.getByTestId("upload-indicator").click(); });
    expect(screen.getAllByText(t("activity.uploading", { count: 3 }))).toHaveLength(2);
    data.global.uploading.transferred_bytes = 20;
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(fetch).toHaveBeenCalledTimes(2);
    const section = screen.getByTestId("upload-activity");
    expect(within(section).getAllByText(t("activity.sent", { sent: formatBytes(20), total: formatBytes(30) }))).toHaveLength(2);
  });

  it("states the speed now, its average and one expected finish with its limiting phase (#755)", async () => {
    const activity = empty();
    activity.queued = { files: 132689, bytes: 514 * 1024 ** 3, transferred_bytes: 0 };
    activity.uploading = { files: 4, bytes: 40 * 1024 ** 2, transferred_bytes: 20 * 1024 ** 2 };
    activity.throughput = measured({ bytes_per_second: 13_200_000, files_per_second: 14.1, average_bytes_per_second: 12_000_000 },
      [{ at: "2026-11-17T16:30:00Z", upload_bytes_per_second: 11_000_000, verification_bytes_per_second: 0 },
        { at: "2026-11-17T16:30:30Z", upload_bytes_per_second: 13_000_000, verification_bytes_per_second: 0 }]);
    const finish = { at: new Date(Date.now() + 2 * 3600_000).toISOString(), seconds: 7200, limited_by: "upload" as const };
    activity.expected_finish = finish;
    show(activity);
    const indicator = await screen.findByTestId("upload-indicator");
    expect(indicator).toHaveAccessibleName(t("activity.indicator_finish", {
      count: 132693, size: formatBytes(514 * 1024 ** 3 + 40 * 1024 ** 2), speed: "13.2 MB/s", time: expectedFinishShort(finish) }));
    // The tooltip says what is counted and over which windows.
    expect(indicator.title).toContain(t("activity.current_work", { seconds: 20, minutes: 5 }));
    await openPanel();
    expect(screen.getAllByTestId("activity-finish")[0]).toHaveTextContent(expectedFinishText(finish, t));
    expect(screen.getAllByTestId("activity-finish")[0]).toHaveTextContent("limited by the upload");
    const rate = screen.getAllByTestId("activity-rate")[0];
    expect(rate).toHaveTextContent("Upload to the cloud: now 13.2 MB/s · 105.6 Mbit/s · 14.1 files/s · last 5 min average 12.0 MB/s");
    expect(screen.getAllByTestId("sparkline-upload")[0]).toHaveAccessibleName(t("estimate.history_label", { minutes: 15, peak: "13.0 MB/s" }));
  });

  it("says since when nothing has moved instead of a finish (#759)", async () => {
    const activity = empty();
    activity.queued = { files: 89729, bytes: 90 * 1024 ** 3, transferred_bytes: 0 };
    const since = new Date(Date.now() - 20 * 60_000).toISOString();
    activity.stopped_since = since;
    show(activity);
    const indicator = await screen.findByTestId("upload-indicator");
    expect(indicator).toHaveAccessibleName(t("activity.indicator_stopped", {
      count: 89729, size: formatBytes(90 * 1024 ** 3), time: stoppedShort(since) }));
    await openPanel();
    expect(screen.getAllByTestId("activity-stopped")[0]).toHaveTextContent(stoppedText(since, t));
    expect(screen.getAllByTestId("activity-stopped")[0]).toHaveTextContent(/^Stopped since /);
    expect(screen.queryByTestId("activity-finish")).not.toBeInTheDocument();
  });

  it("shows the speed now without a finish before the average is measured (#755)", async () => {
    const activity = empty(); activity.uploading = { files: 2, bytes: 20, transferred_bytes: 1 };
    activity.throughput = measured({ bytes_per_second: 2_500_000, files_per_second: 0 });
    show(activity);
    expect(await screen.findByTestId("upload-indicator")).toHaveAccessibleName(t("activity.indicator_speed", {
      count: 2, size: formatBytes(20), speed: "2.5 MB/s" }));
    await openPanel();
    expect(screen.queryByTestId("activity-finish")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sparkline-upload")).not.toBeInTheDocument();
  });

  it("says the speed is not measured yet instead of inventing one (#711)", async () => {
    const activity = empty(); activity.queued = { files: 5, bytes: 50, transferred_bytes: 0 };
    show(activity);
    await openPanel();
    expect(screen.getAllByText(t("activity.rate_unknown")).length).toBeGreaterThan(0);
    expect(screen.queryByTestId("activity-rate")).not.toBeInTheDocument();
  });

  it("lists a Paused Vault's queue apart, outside the indicator's count (#711)", async () => {
    const paused = empty(); paused.queued = { files: 21709, bytes: 2048, transferred_bytes: 0 };
    show(empty(), undefined, [{ ...paused, vault_id: 3, vault_name: "Test686", paused: true }]);
    expect(await screen.findByTestId("upload-indicator")).toHaveAccessibleName(t("activity.paused_only", { count: 21709 }));
    await openPanel();
    expect(screen.getByText(t("activity.idle"))).toBeInTheDocument();
    await userEvent.setup().click(screen.getByText(t("activity.paused_heading")));
    const list = screen.getByTestId("paused-vaults-activity");
    expect(within(list).getByRole("link", { name: "Test686" })).toBeVisible();
    expect(within(list).getByText(t("activity.paused"))).toBeVisible();
    expect(screen.queryByText(t("activity.by_vault"))).not.toBeInTheDocument();
  });

  it("says uploads are slowed down while the server is busy (#713)", async () => {
    const activity = empty(); activity.queued = { files: 12, bytes: 4096, transferred_bytes: 0 };
    show(activity, undefined, [], { throttled: true, since: "2026-11-06T09:59:00Z" });
    expect(await screen.findByTestId("activity-server-busy-badge")).toHaveTextContent(t("activity.server_busy"));
    const panel = await openPanel();
    const notice = within(panel).getByTestId("activity-server-busy");
    expect(within(notice).getByText(t("activity.server_busy"))).toBeVisible();
    expect(within(notice).getByText(t("activity.server_busy_detail"))).toBeVisible();
  });

  it("shows no server notice when uploads are not held back (#713)", async () => {
    const activity = empty(); activity.queued = { files: 12, bytes: 4096, transferred_bytes: 0 };
    show(activity, undefined, [], { throttled: false, since: null });
    await openPanel();
    expect(screen.queryByTestId("activity-server-busy")).not.toBeInTheDocument();
    expect(screen.queryByTestId("activity-server-busy-badge")).not.toBeInTheDocument();
  });
});
