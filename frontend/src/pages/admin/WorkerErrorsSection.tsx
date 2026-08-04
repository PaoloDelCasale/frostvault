/* eslint-disable react-refresh/only-export-components -- focused grouping helpers share this page's error model. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  fetchAdminVaults,
  fetchAdminWorkerErrors,
  type AdminVault,
  type AdminWorkerError,
} from "@/api";
import { Badge } from "@/components/Badge";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

const EXPECTED_ENVIRONMENT_MESSAGES = [
  "rclone configuration not found",
  "s3 bucket name is not configured",
] as const;

type WorkerErrorDisposition = "expected_environment" | "actionable";

export type WorkerErrorGroup = {
  key: string;
  component: string;
  classification: string;
  message: string;
  vaultId: number | null;
  operation: string;
  items: AdminWorkerError[];
  count: number;
  first: AdminWorkerError;
  latest: AdminWorkerError;
};

function detailString(error: AdminWorkerError, key: string): string {
  const value = error.detail?.[key];
  return typeof value === "string" ? value.trim() : "";
}

/** The worker's event is the stable operation context; components are the fallback. */
export function workerErrorEventContext(error: AdminWorkerError): string {
  return detailString(error, "event") || detailString(error, "operation");
}

export function workerErrorOperation(error: AdminWorkerError): string {
  return workerErrorEventContext(error) || error.component.trim();
}

/**
 * These are the two documented local placeholder-environment failures. Keep
 * this deliberately narrow: a generic configuration error still needs an
 * administrator to investigate it.
 */
export function isExpectedEnvironmentError(error: AdminWorkerError): boolean {
  const message = error.message.toLowerCase();
  return EXPECTED_ENVIRONMENT_MESSAGES.some((needle) => message.includes(needle));
}

export function workerErrorDisposition(
  error: AdminWorkerError,
): WorkerErrorDisposition {
  return isExpectedEnvironmentError(error)
    ? "expected_environment"
    : "actionable";
}

function timestampValue(value: string): number {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function occurrenceOrder(left: AdminWorkerError, right: AdminWorkerError): number {
  const byTimestamp = timestampValue(left.created_at) - timestampValue(right.created_at);
  return byTimestamp || left.id - right.id;
}

/**
 * Group only on durable error context. In particular, job IDs and exception
 * types in detail are intentionally not keys: they vary between occurrences
 * of the same operational failure and would defeat deduplication.
 */
export function groupWorkerErrors(
  items: AdminWorkerError[],
): WorkerErrorGroup[] {
  const groups = new Map<string, WorkerErrorGroup>();

  for (const item of items) {
    const eventContext = workerErrorEventContext(item);
    const operation = workerErrorOperation(item);
    const key = JSON.stringify([
      item.component,
      item.classification,
      item.message,
      item.vault_id ?? null,
      eventContext,
    ]);
    const existing = groups.get(key);
    if (existing) {
      existing.items.push(item);
      if (occurrenceOrder(existing.first, item) > 0) existing.first = item;
      if (occurrenceOrder(existing.latest, item) < 0) existing.latest = item;
      existing.count += 1;
      continue;
    }

    groups.set(key, {
      key,
      component: item.component,
      classification: item.classification,
      message: item.message,
      vaultId: item.vault_id ?? null,
      operation,
      items: [item],
      count: 1,
      first: item,
      latest: item,
    });
  }

  return [...groups.values()].sort(
    (left, right) => occurrenceOrder(right.latest, left.latest),
  );
}

function vaultLabel(
  vaultId: number | null,
  vaults: AdminVault[],
  translate: (key: string, params?: Record<string, unknown>) => string,
): string {
  if (vaultId === null) return translate("admin.worker_errors_no_vault");
  const vault = vaults.find((candidate) => candidate.id === vaultId);
  return vault?.name || translate("admin.worker_errors_vault_fallback", { id: vaultId });
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export function WorkerErrorsSection() {
  const { t } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [items, setItems] = useState<AdminWorkerError[]>([]);
  const [vaults, setVaults] = useState<AdminVault[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetchAdminWorkerErrors();
      const nextItems = response.items ?? [];
      setItems(nextItems);

      // Vault names are not part of the existing read-only worker-error
      // response. Only request the inventory when it can improve a label;
      // stale/deleted IDs still receive a deterministic fallback below.
      if (nextItems.some((item) => item.vault_id !== null)) {
        try {
          const vaultResponse = await fetchAdminVaults();
          setVaults(vaultResponse.items ?? []);
        } catch {
          setVaults([]);
        }
      } else {
        setVaults([]);
      }
    } catch (reason) {
      setError(
        errorMessage(
          reason,
          translateRef.current("admin.worker_errors_error"),
        ),
      );
      setItems([]);
      setVaults([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const groups = useMemo(() => groupWorkerErrors(items), [items]);

  return (
    <section aria-labelledby="admin-worker-errors-heading" className="grid gap-4">
      <div>
        <h2 id="admin-worker-errors-heading" className="text-xl font-bold">
          {t("admin.worker_errors_heading")}
        </h2>
        <p className="mt-1 text-sm text-muted">
          {t("admin.worker_errors_subtitle")}
        </p>
      </div>

      <Panel className="border border-line bg-canvas p-4">
        <p className="text-sm text-muted">{t("admin.worker_errors_read_only")}</p>
      </Panel>

      {loading ? (
        <p role="status" className="text-sm text-muted">
          {t("admin.worker_errors_loading")}
        </p>
      ) : error ? (
        <div className="grid gap-3">
          <p role="alert" className="text-sm font-bold text-[var(--state-local-fg)]">
            {error}
          </p>
          <div>
            <Button type="button" variant="secondary" onClick={() => void load()}>
              {t("admin.worker_errors_retry")}
            </Button>
          </div>
        </div>
      ) : groups.length === 0 ? (
        <p className="text-sm text-muted">{t("admin.worker_errors_empty")}</p>
      ) : (
        <ul className="grid gap-4" aria-label={t("admin.worker_errors_list_label")}>
          {groups.map((group) => {
            const disposition = workerErrorDisposition(group.latest);
            const expected = disposition === "expected_environment";
            const operation = group.operation || t("admin.worker_errors_unknown_operation");
            return (
              <li key={group.key}>
                <Panel className="p-5">
                  <article aria-labelledby={`worker-error-${group.key}`}>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <h3
                          id={`worker-error-${group.key}`}
                          className="break-words text-lg font-bold"
                        >
                          {operation}
                        </h3>
                        <p className="mt-1 break-words text-xs text-muted">
                          {t("admin.worker_errors_component")}: {group.component}
                        </p>
                      </div>
                      <Badge
                        state={expected ? "both" : "missing"}
                        label={
                          expected
                            ? t("admin.worker_errors_expected_environment")
                            : t("admin.worker_errors_actionable")
                        }
                      />
                    </div>

                    <dl className="mt-4 grid gap-3 sm:grid-cols-2">
                      <div>
                        <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                          {t("admin.worker_errors_timestamp")}
                        </dt>
                        <dd className="mt-1 break-all text-sm">
                          <time dateTime={group.latest.created_at}>
                            {group.latest.created_at}
                          </time>
                        </dd>
                      </div>
                      <div>
                        <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                          {t("admin.worker_errors_operation")}
                        </dt>
                        <dd className="mt-1 break-words text-sm">{operation}</dd>
                      </div>
                      <div>
                        <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                          {t("admin.worker_errors_vault")}
                        </dt>
                        <dd className="mt-1 break-words text-sm">
                          {vaultLabel(group.vaultId, vaults, t)}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                          {t("admin.worker_errors_classification")}
                        </dt>
                        <dd className="mt-1 break-words text-sm">{group.classification}</dd>
                      </div>
                    </dl>

                    <div className="mt-4 rounded-[10px] border border-line bg-canvas p-3">
                      <p className="text-xs font-extrabold tracking-wide text-muted uppercase">
                        {t("admin.worker_errors_message")}
                      </p>
                      <p className="mt-1 break-words text-sm">{group.message}</p>
                    </div>

                    <p className="mt-4 text-sm text-muted">
                      {t("admin.worker_errors_occurrences", { count: group.count })}
                      <span className="mx-2" aria-hidden="true">·</span>
                      {t("admin.worker_errors_first_seen")}: {group.first.created_at}
                      <span className="mx-2" aria-hidden="true">·</span>
                      {t("admin.worker_errors_latest_seen")}: {group.latest.created_at}
                    </p>
                  </article>
                </Panel>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
