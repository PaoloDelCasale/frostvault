/* eslint-disable react-refresh/only-export-components -- focused filter helpers share the panel's bounded-window model. */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import {
  fetchVaultAuditEvents,
  type AuditEvent,
  type VaultMember,
} from "@/api";
import { Badge, type BadgeState } from "@/components/Badge";
import { FormField, FormInput, FormSelect } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

/** The API has no paging or query parameters and returns at most this many rows. */
export const AUDIT_EVENT_WINDOW_LIMIT = 100;

export type AuditEventFilters = {
  actor: string;
  action: string;
  from: string;
  to: string;
};

const EMPTY_AUDIT_EVENT_FILTERS: AuditEventFilters = {
  actor: "",
  action: "",
  from: "",
  to: "",
};

function actorFilterValue(event: AuditEvent): string {
  return event.actor_user_id === null ? "system" : String(event.actor_user_id);
}

function eventDate(value: string): string | null {
  const match = /^(\d{4}-\d{2}-\d{2})/.exec(value);
  return match?.[1] ?? null;
}

/** Filters never refetch: they only narrow the already-loaded newest event window. */
export function filterAuditEvents(
  events: AuditEvent[],
  filters: AuditEventFilters,
): AuditEvent[] {
  return events.filter((event) => {
    if (filters.actor && actorFilterValue(event) !== filters.actor) return false;
    if (filters.action && event.event !== filters.action) return false;

    if (filters.from || filters.to) {
      const date = eventDate(event.created_at);
      if (date === null) return false;
      if (filters.from && date < filters.from) return false;
      if (filters.to && date > filters.to) return false;
    }
    return true;
  });
}

function eventWindow(events: AuditEvent[]): AuditEvent[] {
  return events.slice(0, AUDIT_EVENT_WINDOW_LIMIT);
}

function outcomeBadgeState(outcome: string | null): BadgeState {
  const value = outcome?.trim().toLowerCase();
  if (!value) return "unsupported";
  if (
    [
      "success",
      "succeeded",
      "created",
      "updated",
      "activated",
      "requested",
      "queued",
      "completed",
      "verified",
    ].includes(value)
  ) {
    return "both";
  }
  if (["warning", "warned", "partial"].includes(value)) return "mixed";
  if (["failure", "failed", "blocked", "denied", "cancelled"].includes(value)) {
    return "missing";
  }
  return "cloud_only";
}

function detailValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value === null) return "null";
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

function relatedVaultFile(event: AuditEvent): string | null {
  for (const key of ["vault_file_id", "path", "new_path", "old_path"]) {
    const value = event.detail?.[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function auditEventDomId(prefix: string, eventId: number): string {
  return `${prefix}-event-${eventId}`;
}

type AuditEventCardsProps = {
  events: AuditEvent[];
  resolveActor: (event: AuditEvent) => string;
  resolveVault?: (event: AuditEvent) => string;
  showVault: boolean;
  idPrefix: string;
};

/**
 * A card-only rendering keeps wide audit records usable below `sm`. It owns
 * only local filtering state; loaders remain responsible for authorization and
 * the one bounded endpoint request.
 */
export function AuditEventCards({
  events,
  resolveActor,
  resolveVault,
  showVault,
  idPrefix,
}: AuditEventCardsProps) {
  const { t } = useI18n();
  const filtersId = useId();
  const [filters, setFilters] = useState<AuditEventFilters>(EMPTY_AUDIT_EVENT_FILTERS);

  const actorOptions = useMemo(() => {
    const options = new Map<string, string>();
    for (const event of events) {
      options.set(actorFilterValue(event), resolveActor(event));
    }
    return [...options.entries()].sort(([, left], [, right]) => left.localeCompare(right));
  }, [events, resolveActor]);

  const actionOptions = useMemo(
    () => [...new Set(events.map((event) => event.event))].sort((left, right) =>
      left.localeCompare(right),
    ),
    [events],
  );
  const filteredEvents = useMemo(
    () => filterAuditEvents(events, filters),
    [events, filters],
  );

  return (
    <>
      <Panel className="grid gap-4 border border-line bg-canvas p-4 sm:p-5">
        <div className="grid gap-1">
          <p className="text-sm font-bold text-ink">
            {t("audit.loaded_window", {
              count: events.length,
              limit: AUDIT_EVENT_WINDOW_LIMIT,
            })}
          </p>
          <p className="text-sm text-muted">{t("audit.local_filters_notice")}</p>
        </div>

        <fieldset className="grid gap-3 border-0 p-0">
          <legend className="text-sm font-bold text-ink">{t("audit.filters")}</legend>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            <FormField label={t("audit.actor")} htmlFor={`${filtersId}-actor`}>
              <FormSelect
                id={`${filtersId}-actor`}
                value={filters.actor}
                onChange={(event) =>
                  setFilters((current) => ({ ...current, actor: event.target.value }))
                }
              >
                <option value="">{t("audit.all_actors")}</option>
                {actorOptions.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </FormSelect>
            </FormField>

            <FormField label={t("audit.action")} htmlFor={`${filtersId}-action`}>
              <FormSelect
                id={`${filtersId}-action`}
                value={filters.action}
                onChange={(event) =>
                  setFilters((current) => ({ ...current, action: event.target.value }))
                }
              >
                <option value="">{t("audit.all_actions")}</option>
                {actionOptions.map((action) => (
                  <option key={action} value={action}>
                    {action}
                  </option>
                ))}
              </FormSelect>
            </FormField>

            <FormField label={t("audit.from_date")} htmlFor={`${filtersId}-from`}>
              <FormInput
                id={`${filtersId}-from`}
                type="date"
                value={filters.from}
                max={filters.to || undefined}
                onChange={(event) =>
                  setFilters((current) => ({ ...current, from: event.target.value }))
                }
              />
            </FormField>

            <FormField label={t("audit.to_date")} htmlFor={`${filtersId}-to`}>
              <FormInput
                id={`${filtersId}-to`}
                type="date"
                value={filters.to}
                min={filters.from || undefined}
                onChange={(event) =>
                  setFilters((current) => ({ ...current, to: event.target.value }))
                }
              />
            </FormField>

            <div className="flex items-end">
              <Button
                type="button"
                variant="secondary"
                className="w-full"
                onClick={() => setFilters({ ...EMPTY_AUDIT_EVENT_FILTERS })}
              >
                {t("audit.clear_filters")}
              </Button>
            </div>
          </div>
        </fieldset>
      </Panel>

      {events.length === 0 ? (
        <p className="text-sm text-muted">{t("audit.empty")}</p>
      ) : filteredEvents.length === 0 ? (
        <p className="text-sm text-muted">{t("audit.no_matches")}</p>
      ) : (
        <ul className="grid gap-4" aria-label={t("audit.list_label")}>
          {filteredEvents.map((event) => {
            const headingId = auditEventDomId(idPrefix, event.id);
            const detailEntries = Object.entries(event.detail ?? {});
            const vaultFile = relatedVaultFile(event);
            const outcome = event.outcome?.trim() || t("audit.outcome_not_recorded");
            return (
              <li key={event.id}>
                <Panel className="p-4 sm:p-5">
                  <article aria-labelledby={headingId}>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <h3 id={headingId} className="break-words text-lg font-bold">
                          {event.event}
                        </h3>
                        <time
                          dateTime={event.created_at}
                          className="mt-1 block break-all text-xs text-muted"
                        >
                          {event.created_at}
                        </time>
                      </div>
                      <Badge state={outcomeBadgeState(event.outcome)} label={outcome} />
                    </div>

                    <dl className="mt-4 grid gap-3 sm:grid-cols-2">
                      <div>
                        <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                          {t("audit.actor")}
                        </dt>
                        <dd className="mt-1 break-words text-sm">{resolveActor(event)}</dd>
                      </div>
                      <div>
                        <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                          {t("audit.action")}
                        </dt>
                        <dd className="mt-1 break-words text-sm">{event.event}</dd>
                      </div>
                      <div>
                        <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                          {t("audit.timestamp")}
                        </dt>
                        <dd className="mt-1 break-all text-sm">
                          <time dateTime={event.created_at}>{event.created_at}</time>
                        </dd>
                      </div>
                      <div>
                        <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                          {t("audit.outcome")}
                        </dt>
                        <dd className="mt-1 break-words text-sm">{outcome}</dd>
                      </div>
                      {showVault && resolveVault ? (
                        <div>
                          <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                            {t("audit.vault")}
                          </dt>
                          <dd className="mt-1 break-words text-sm">{resolveVault(event)}</dd>
                        </div>
                      ) : null}
                      {vaultFile ? (
                        <div className={showVault ? "" : "sm:col-span-2"}>
                          <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                            {t("audit.vault_file")}
                          </dt>
                          <dd className="mt-1 break-all text-sm">{vaultFile}</dd>
                        </div>
                      ) : null}
                    </dl>

                    <details className="mt-4 rounded-[10px] border border-line bg-canvas">
                      <summary className="flex min-h-11 cursor-pointer items-center px-3 text-sm font-bold text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-green">
                        {t("audit.details")}
                      </summary>
                      <div className="border-t border-line p-3">
                        <dl className="grid gap-3 text-sm sm:grid-cols-2">
                          <div>
                            <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                              {t("audit.event_id")}
                            </dt>
                            <dd className="mt-1 break-all">{event.id}</dd>
                          </div>
                          <div>
                            <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                              {t("audit.visibility")}
                            </dt>
                            <dd className="mt-1 break-words">{event.visibility}</dd>
                          </div>
                          {event.job_id !== null ? (
                            <div>
                              <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                                {t("audit.job_id")}
                              </dt>
                              <dd className="mt-1 break-all">{event.job_id}</dd>
                            </div>
                          ) : null}
                          {event.correlation_id ? (
                            <div>
                              <dt className="text-xs font-extrabold tracking-wide text-muted uppercase">
                                {t("audit.correlation_id")}
                              </dt>
                              <dd className="mt-1 break-all font-mono text-xs">
                                {event.correlation_id}
                              </dd>
                            </div>
                          ) : null}
                          {detailEntries.length === 0 ? (
                            <div className="sm:col-span-2">
                              <dt className="sr-only">{t("audit.details")}</dt>
                              <dd className="text-muted">{t("audit.details_empty")}</dd>
                            </div>
                          ) : (
                            detailEntries.map(([key, value]) => (
                              <div key={key} className="sm:col-span-2">
                                <dt className="break-all font-mono text-xs font-extrabold text-muted">
                                  {key}
                                </dt>
                                <dd className="mt-1 break-words whitespace-pre-wrap">
                                  {detailValue(value)}
                                </dd>
                              </div>
                            ))
                          )}
                        </dl>
                      </div>
                    </details>
                  </article>
                </Panel>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

export type VaultAuditEventsPanelProps = {
  members: VaultMember[];
};

/** Owner-only caller supplies the already-authorized current-Vault membership list. */
export function VaultAuditEventsPanel({ members }: VaultAuditEventsPanelProps) {
  const { t } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetchVaultAuditEvents();
      setEvents(eventWindow(response.events ?? []));
    } catch (reason) {
      setEvents([]);
      setError(errorMessage(reason, translateRef.current("audit.load_error")));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function resolveActor(event: AuditEvent): string {
    if (event.actor_user_id === null) return t("audit.actor_system");
    const member = members.find((candidate) => candidate.id === event.actor_user_id);
    return (
      member?.display_name
      || member?.username
      || t("audit.actor_fallback", { id: event.actor_user_id })
    );
  }

  return (
    <section
      data-panel="audit-events"
      aria-labelledby="vault-audit-events-heading"
      aria-busy={loading}
      className="grid gap-4"
    >
      <div>
        <h2 id="vault-audit-events-heading" className="text-lg font-bold text-ink">
          {t("access.audit_heading")}
        </h2>
        <p className="mt-1 text-sm text-muted">{t("access.audit_subtitle")}</p>
      </div>

      {loading ? (
        <p role="status" className="text-sm text-muted">
          {t("audit.loading")}
        </p>
      ) : error ? (
        <div className="grid gap-3">
          <p role="alert" className="break-words text-sm font-bold text-[var(--state-local-fg)]">
            {error}
          </p>
          <div>
            <Button type="button" variant="secondary" onClick={() => void load()}>
              {t("audit.retry")}
            </Button>
          </div>
        </div>
      ) : (
        <AuditEventCards
          events={events}
          resolveActor={resolveActor}
          showVault={false}
          idPrefix="vault-audit"
        />
      )}
    </section>
  );
}
