import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { configureApiClient, resetApiClientForTests, type ApiFetch } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { problemLink } from "@/lib/problemLink";
import { OverviewPage } from "@/pages/overview/OverviewPage";
import { loadCatalog } from "./vault-settings-harness";
import { jsonResponse } from "./support/http";
import { renderWithQueryClient } from "./support/queryClient";

afterEach(resetApiClientForTests);

// #735: a new Vault is active only once its own bucket is created and verified.
const failedBucket = {
  vault_id: 7, bucket: "frostvault-travel-a1b2c3d4", region: "eu-west-1", dedicated: true, state: "failed",
  stage: "reserved", attempts: 2, last_attempt_at: "2026-05-01T10:00:00+00:00",
  error: "S3 refused s3:CreateBucket (AccessDenied)", retention_lock_days: null,
  active_at: null, active_in_seconds: null,
};

function setup(retryState: "failed" | "ready" = "failed", bucket: Record<string, unknown> = failedBucket) {
  let cancelled = false;
  const messages = loadCatalog("en");
  const calls: { url: string; method: string }[] = [];
  configureApiClient({ fetch: vi.fn<ApiFetch>(async (input, init) => {
    const url = String(input); const method = init?.method ?? "GET";
    calls.push({ url, method });
    if (url.startsWith("/api/i18n/catalog")) return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    if (url.startsWith("/api/stats")) return jsonResponse({ states: { protected: 1 }, protection_state: "protected", storage: {} });
    if (url === "/api/vaults/7/bucket/retry" && method === "POST") return jsonResponse(
      retryState === "ready" ? { ...failedBucket, state: "ready", stage: null, attempts: 0, error: null } : failedBucket);
    if (url === "/api/vaults/7/creation/cancel" && method === "POST") {
      cancelled = true;
      return jsonResponse({ vault_id: 7, vault_name: "Travel", bucket: "frostvault-travel-a1b2c3d4", region: "eu-west-1", bucket_left: "possible" });
    }
    if (url.startsWith("/api/general/vaults")) return jsonResponse({ items: [
      { id: 1, uuid: "v1", name: "Photos", lifecycle: "active", source_root: "/sources/v1", s3_prefix: "", bucket_provisioning: null },
      ...(cancelled ? [] : [{ id: 7, uuid: "v7", name: "Travel", lifecycle: "provisioning", source_root: "/sources/v7", s3_prefix: "", bucket_provisioning: bucket }]),
    ] });
    return jsonResponse({}, 404);
  }) });
  renderWithQueryClient(<I18nProvider>
    <OverviewPage vaults={[{ id: 1, uuid: "v1", name: "Photos" }]} />
  </I18nProvider>);
  return { calls, messages };
}

describe("Vault Bucket creation on the Overview (#735)", () => {
  it("lists a Vault whose bucket is not ready, with the failure, outside the Vault table", async () => {
    const { messages } = setup();
    const section = await screen.findByTestId("overview-creating");
    expect(section).toHaveTextContent("Vaults being created (1)");
    const item = within(section).getByTestId("overview-creating-vault");
    expect(item).toHaveTextContent("Travel");
    expect(item).toHaveTextContent(messages["overview.creating_failed"]);
    expect(item).toHaveTextContent("frostvault-travel-a1b2c3d4: S3 refused s3:CreateBucket (AccessDenied)");
    expect(within(await screen.findByTestId("overview-table")).queryByText("Travel")).toBeNull();
  });

  it("retries the creation from the Overview", async () => {
    const { calls, messages } = setup("ready");
    const user = userEvent.setup();
    const section = await screen.findByTestId("overview-creating");
    await user.click(within(section).getByRole("button", { name: messages["ui.vault_create.bucket_retry"] }));
    await waitFor(() => expect(calls.some((call) => call.url === "/api/vaults/7/bucket/retry" && call.method === "POST")).toBe(true));
  });

  it("says when a verified Vault becomes active, with nothing to retry", async () => {
    setup("failed", { ...failedBucket, state: "settling", stage: "verified", attempts: 0, error: null,
      active_at: "2026-05-01T10:15:00+00:00", active_in_seconds: 61 });
    const item = within(await screen.findByTestId("overview-creating")).getByTestId("overview-creating-vault");
    expect(item).toHaveTextContent("Active in about 2 minutes");
    expect(within(item).queryByRole("button", { name: "Retry now" })).toBeNull();
  });

  it("cancels a creation from the Overview and names the possible bucket", async () => {
    const { calls, messages } = setup();
    const user = userEvent.setup();
    const section = await screen.findByTestId("overview-creating");
    await user.click(within(section).getByRole("button", { name: messages["ui.vault_create.cancel_creation"] }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: messages["ui.vault_create.cancel_creation"] }));
    expect(await screen.findByTestId("overview-creation-cancelled")).toHaveTextContent(
      "may have reached AWS: check whether the bucket frostvault-travel-a1b2c3d4 (eu-west-1) exists");
    expect(calls.filter((call) => call.url === "/api/vaults/7/creation/cancel")).toHaveLength(1);
    await waitFor(() => expect(screen.queryByTestId("overview-creating")).toBeNull());
  });

  it("links a Vault Bucket Problem to the Vaults being created", () => {
    expect(problemLink(7, { vault_bucket: true, bucket: "frostvault-travel-a1b2c3d4" })).toBe("/#creating");
  });
});
