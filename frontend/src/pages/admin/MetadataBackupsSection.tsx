/* eslint-disable react-refresh/only-export-components -- outcome helper is tested with the section. */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  downloadAdminMetadataBackup,
  fetchAdminMetadataBackups,
  runAdminMetadataBackup,
  type MetadataBackupRun,
  type MetadataBackupStatus,
} from "@/api";
import { Badge, type BadgeState } from "@/components/Badge";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

export type MetadataBackupOutcome =
  | "full_off_host"
  | "local_only"
  | "failed"
  | "pending"
  | "unknown";

const ELIGIBLE_DOWNLOAD_STATUSES = new Set(["succeeded", "verified"]);

export function metadataBackupOutcome(
  run: Pick<MetadataBackupRun, "status" | "s3_key">,
): MetadataBackupOutcome {
  const status = run.status.toLowerCase();
  if (status === "failed") return "failed";
  if (status === "pending") return "pending";
  if (ELIGIBLE_DOWNLOAD_STATUSES.has(status)) {
    return run.s3_key ? "full_off_host" : "local_only";
  }
  return "unknown";
}

function outcomeBadgeState(outcome: MetadataBackupOutcome): BadgeState {
  switch (outcome) {
    case "full_off_host":
      return "both";
    case "local_only":
      return "local_only";
    case "pending":
      return "restoring";
    case "failed":
    case "unknown":
      return "missing";
  }
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function isValidChecksum(value: string | null): boolean {
  return Boolean(value && /^[a-f0-9]{64}$/i.test(value.trim()));
}

function timestamp(run: MetadataBackupRun): string {
  return run.created_at;
}

export function MetadataBackupsSection() {
  const { t } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [status, setStatus] = useState<MetadataBackupStatus | null>(null);
  const [runs, setRuns] = useState<MetadataBackupRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [runPending, setRunPending] = useState(false);
  const [runError, setRunError] = useState("");
  const [runMessage, setRunMessage] = useState("");
  const [downloadingId, setDownloadingId] = useState<number | null>(null);
  const [downloadError, setDownloadError] = useState("");

  const load = useCallback(async (): Promise<boolean> => {
    setLoading(true);
    setListError("");
    try {
      const response = await fetchAdminMetadataBackups();
      setStatus(response.status);
      setRuns(response.runs ?? []);
      return true;
    } catch (reason) {
      setStatus(null);
      setRuns([]);
      setListError(
        errorMessage(reason, translateRef.current("admin.metadata_backups_load_error")),
      );
      return false;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleRun() {
    setRunPending(true);
    setRunError("");
    setRunMessage("");
    let runSucceeded = false;
    try {
      await runAdminMetadataBackup();
      runSucceeded = true;
    } catch (reason) {
      setRunError(
        errorMessage(reason, translateRef.current("admin.metadata_backups_run_error")),
      );
    } finally {
      // The endpoint is synchronous. Refresh on both success and failure so a
      // recorded failed run is visible without inventing a polling API.
      const refreshed = await load();
      if (runSucceeded && refreshed) {
        setRunMessage(translateRef.current("admin.metadata_backups_run_success"));
      }
      setRunPending(false);
    }
  }

  async function handleDownload(run: MetadataBackupRun) {
    setDownloadingId(run.id);
    setDownloadError("");
    try {
      const artifact = await downloadAdminMetadataBackup(run.id);
      const expectedChecksum = run.digest_sha256?.trim().toLowerCase() ?? null;
      if (
        isValidChecksum(expectedChecksum) &&
        artifact.checksumSha256 &&
        artifact.checksumSha256 !== expectedChecksum
      ) {
        throw new Error(t("admin.metadata_backups_checksum_mismatch"));
      }

      if (typeof URL.createObjectURL !== "function") {
        throw new Error(t("admin.metadata_backups_download_error"));
      }
      const objectUrl = URL.createObjectURL(artifact.blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = artifact.filename;
      anchor.rel = "noopener";
      anchor.style.display = "none";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
    } catch (reason) {
      setDownloadError(
        errorMessage(
          reason,
          translateRef.current("admin.metadata_backups_download_error"),
        ),
      );
    } finally {
      setDownloadingId(null);
    }
  }

  return (
    <section
      aria-labelledby="admin-metadata-backups-heading"
      aria-busy={loading || runPending}
      className="grid gap-4"
    >
      <div>
        <h2 id="admin-metadata-backups-heading" className="text-xl font-bold">
          {t("admin.metadata_backups_heading")}
        </h2>
        <p className="mt-1 text-sm text-muted">
          {t("admin.metadata_backups_subtitle")}
        </p>
      </div>

      <Panel className="grid gap-4 p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h3 className="text-lg font-bold">
              {t("admin.metadata_backups_run_heading")}
            </h3>
            <p className="mt-1 text-sm text-muted">
              {t("admin.metadata_backups_run_help")}
            </p>
          </div>
          <Button
            type="button"
            variant="primary"
            disabled={runPending}
            onClick={() => void handleRun()}
          >
            {runPending
              ? t("admin.metadata_backups_running")
              : t("admin.metadata_backups_run")}
          </Button>
        </div>
        {runPending ? (
          <div role="status" aria-live="polite" className="grid gap-2">
            <progress
              aria-label={t("admin.metadata_backups_progress")}
              className="h-2 w-full accent-green"
            />
            <p className="text-sm font-bold text-muted">
              {t("admin.metadata_backups_progress")}
            </p>
          </div>
        ) : null}
        {runMessage ? (
          <p role="status" aria-live="polite" className="text-sm font-bold text-green">
            {runMessage}
          </p>
        ) : null}
        {runError ? (
          <p role="alert" className="break-words text-sm font-bold text-[var(--state-local-fg)]">
            {runError}
          </p>
        ) : null}
      </Panel>

      {status ? (
        <Panel className="p-5">
          <dl className="grid gap-3 sm:grid-cols-2">
            <div>
              <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                {t("admin.metadata_backups_successful_runs")}
              </dt>
              <dd className="mt-1 text-2xl font-bold">{status.succeeded_count}</dd>
            </div>
            <div>
              <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                {t("admin.metadata_backups_failed_runs")}
              </dt>
              <dd className="mt-1 text-2xl font-bold">{status.failed_count}</dd>
            </div>
          </dl>
        </Panel>
      ) : null}

      {loading ? (
        <p role="status" className="text-sm text-muted">
          {t("admin.metadata_backups_loading")}
        </p>
      ) : listError ? (
        <div className="grid gap-3">
          <p role="alert" className="break-words text-sm font-bold text-[var(--state-local-fg)]">
            {listError}
          </p>
          <div>
            <Button type="button" variant="secondary" onClick={() => void load()}>
              {t("admin.metadata_backups_retry")}
            </Button>
          </div>
        </div>
      ) : runs.length === 0 ? (
        <p className="text-sm text-muted">{t("admin.metadata_backups_empty")}</p>
      ) : (
        <ul
          aria-label={t("admin.metadata_backups_list_label")}
          className="grid gap-4"
        >
          {runs.map((run) => {
            const outcome = metadataBackupOutcome(run);
            const eligible = ELIGIBLE_DOWNLOAD_STATUSES.has(run.status.toLowerCase());
            const isDownloading = downloadingId === run.id;
            const headingId = `metadata-backup-${run.id}`;
            return (
              <li key={run.id}>
                <Panel className="p-5">
                  <article aria-labelledby={headingId}>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <h3 id={headingId} className="text-lg font-bold">
                          {t("admin.metadata_backups_run_label", { id: run.id })}
                        </h3>
                        <p className="mt-1 text-xs text-muted">
                          {run.backend}
                        </p>
                      </div>
                      <Badge
                        state={outcomeBadgeState(outcome)}
                        label={t(`admin.metadata_backups_outcome_${outcome}`)}
                      />
                    </div>

                    <dl className="mt-4 grid gap-3 sm:grid-cols-2">
                      <div>
                        <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                          {t("admin.metadata_backups_timestamp")}
                        </dt>
                        <dd className="mt-1 break-all text-sm">
                          <time dateTime={timestamp(run)}>{timestamp(run)}</time>
                        </dd>
                      </div>
                      <div>
                        <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                          {t("admin.metadata_backups_verification")}
                        </dt>
                        <dd className="mt-1 break-words text-sm">
                          {run.verified_at ? (
                            <>
                              {t("admin.metadata_backups_verified")}
                              <span className="mx-2" aria-hidden="true">
                                ·
                              </span>
                              <time dateTime={run.verified_at}>{run.verified_at}</time>
                            </>
                          ) : (
                            t("admin.metadata_backups_not_verified")
                          )}
                        </dd>
                      </div>
                      <div className="sm:col-span-2">
                        <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                          {t("admin.metadata_backups_checksum")}
                        </dt>
                        <dd className="mt-1 break-all font-mono text-xs">
                          {run.digest_sha256 ?? t("admin.metadata_backups_checksum_unavailable")}
                        </dd>
                      </div>
                    </dl>

                    {run.error_message ? (
                      <p className="mt-4 break-words rounded-[10px] border border-line bg-canvas p-3 text-sm">
                        <strong>{t("admin.metadata_backups_failure_reason")}: </strong>
                        {run.error_message}
                      </p>
                    ) : null}

                    {eligible ? (
                      <div className="mt-4">
                        <Button
                          type="button"
                          variant="secondary"
                          disabled={downloadingId !== null}
                          onClick={() => void handleDownload(run)}
                        >
                          {isDownloading
                            ? t("admin.metadata_backups_downloading")
                            : t("admin.metadata_backups_download")}
                        </Button>
                      </div>
                    ) : null}
                  </article>
                </Panel>
              </li>
            );
          })}
        </ul>
      )}

      {downloadError ? (
        <p role="alert" className="break-words text-sm font-bold text-[var(--state-local-fg)]">
          {downloadError}
        </p>
      ) : null}
    </section>
  );
}
