import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests } from "@/api";
import type { UploadActivitySummary, UploadThroughput } from "@/api/types";
import { translate } from "@/i18n/translate";
import { VaultHeader } from "@/layout/VaultHeader";
import { expectedFinishText, stoppedText } from "@/lib/throughput";
import { createVaultAccessFetch, jsonResponse, loadCatalog } from "./vault-settings-harness";

afterEach(resetApiClientForTests);
const messages = loadCatalog();
const t = (key: string, params?: Record<string, string | number>) => translate(messages, key, params);
const idle = { files: 0, bytes: 0, transferred_bytes: 0 };

function throughput(): UploadThroughput {
  return {
    sample_seconds: 2, instant_seconds: 20, average_seconds: 300, history_seconds: 900,
    upload: { bytes_per_second: 13_200_000, files_per_second: 14.1, average_bytes_per_second: 12_000_000 },
    verification: { bytes_per_second: 45_000_000, average_bytes_per_second: 40_100_000 },
    history: [
      { at: "2026-11-17T16:30:00Z", upload_bytes_per_second: 11_000_000, verification_bytes_per_second: 41_000_000 },
      { at: "2026-11-17T16:30:30Z", upload_bytes_per_second: 13_000_000, verification_bytes_per_second: 45_000_000 },
    ],
  };
}

function setup({ paused = false, scanning = true, stopped = null as string | null } = {}) {
  const finish = { at: new Date(Date.now() + 14 * 3600_000).toISOString(), seconds: 50400, limited_by: "upload" as const };
  const vault: UploadActivitySummary & { vault_id: number; vault_name: string; paused: boolean } = {
    vault_id: 3, vault_name: "Immich", paused,
    stability_wait: idle, verifying: idle, uploading: idle,
    queued: { files: 132693, bytes: 514 * 1024 ** 3, transferred_bytes: 0 },
    awaiting_admission_files: 0, next_stable_at: null,
    throughput: throughput(), expected_finish: paused || stopped ? null : finish, stopped_since: stopped,
  };
  configureApiClient({ fetch: createVaultAccessFetch({
    "GET /api/general/vaults": () => jsonResponse({ items: [] }),
    "GET /api/stats": () => jsonResponse({ states: { pending: 1 }, protection_state: "pending", storage: {}, runtime: {
      scanning, scan_phase: scanning ? "verification" : null,
      verify_files_total: 137717, verify_files_done: 16055,
      verify_bytes_total: 519.5 * 1024 ** 3, verify_bytes_done: 18.9 * 1024 ** 3,
    } }),
    "GET /api/upload-activity": () => jsonResponse({ observed_at: "2026-11-17T16:33:00Z", global: vault, vaults: [vault] }),
  }) });
  render(<ApiQueryProvider client={createAppQueryClient()}>
    <VaultHeader vaultId={3} vaultName="Immich" active="files" t={t} />
  </ApiQueryProvider>);
  return finish;
}

describe("A Vault's one expected finish and the speeds of its two phases (#755)", () => {
  it("names the finish with its limiting phase, then the upload and the reading from the disk", async () => {
    const finish = setup();
    const panel = await screen.findByTestId("vault-progress");
    expect(await within(panel).findByTestId("vault-reading-line")).toBeInTheDocument();
    expect(within(panel).getByTestId("vault-expected-finish")).toHaveTextContent(expectedFinishText(finish, t));
    expect(within(panel).getByTestId("vault-expected-finish")).toHaveTextContent("limited by the upload");
    expect(within(panel).getByTestId("vault-upload-line")).toHaveTextContent(
      "Upload to the cloud: 132,693 files · 514.0 GiB to upload · now 13.2 MB/s · 105.6 Mbit/s · 14.1 files/s · last 5 min average 12.0 MB/s");
    expect(within(panel).getByTestId("vault-reading-line")).toHaveTextContent(
      "Reading from the disk: now 45.0 MB/s · last 5 min average 40.1 MB/s");
    expect(within(panel).getByTestId("sparkline-upload")).toBeInTheDocument();
    expect(within(panel).getByTestId("sparkline-verification")).toBeInTheDocument();
    // The verification phase no longer carries its own time left.
    expect(screen.getByTestId("scan-verification")).not.toHaveTextContent(/left/);
  });

  it("shows since when the Vault stopped instead of a finish (#759)", async () => {
    const since = new Date(Date.now() - 15 * 60_000).toISOString();
    setup({ scanning: false, stopped: since });
    const line = await screen.findByTestId("vault-stopped");
    expect(line).toHaveTextContent(stoppedText(since, t));
    expect(line).toHaveTextContent(/^Stopped since /);
    expect(screen.queryByTestId("vault-expected-finish")).not.toBeInTheDocument();
  });

  it("shows no reading line without a verifying scan and no finish while paused", async () => {
    setup({ paused: true, scanning: false });
    const line = await screen.findByTestId("vault-upload-line");
    expect(line).toHaveTextContent(t("vault.upload.paused"));
    expect(line).not.toHaveTextContent("MB/s");
    expect(screen.queryByTestId("vault-expected-finish")).not.toBeInTheDocument();
    expect(screen.queryByTestId("vault-reading-line")).not.toBeInTheDocument();
  });
});
