import { useCallback, useEffect, useRef, useState } from "react";

import {
  fetchAdminAuditEvents,
  fetchAdminUsers,
  fetchAdminVaults,
  type AdminUser,
  type AdminVault,
  type AuditEvent,
} from "@/api";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";
import {
  AUDIT_EVENT_WINDOW_LIMIT,
  AuditEventCards,
} from "@/pages/vault-access/AuditEventsPanel";

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

/**
 * Global administration already authorizes the users and Vault inventories.
 * The generic audit response only carries IDs, so failed or stale lookups
 * deliberately leave a deterministic ID label instead of expanding access.
 */
export function AdminAuditEventsSection() {
  const { t } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [vaults, setVaults] = useState<AdminVault[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    setUsers([]);
    setVaults([]);
    try {
      const response = await fetchAdminAuditEvents();
      const nextEvents = (response.events ?? []).slice(0, AUDIT_EVENT_WINDOW_LIMIT);
      setEvents(nextEvents);

      // These are existing admin-authorized endpoints, not new lookup APIs.
      // Name enrichment cannot make the audit view fail: deleted users/Vaults
      // and unavailable inventories remain visible through the ID fallbacks.
      const [nextUsers, nextVaults] = await Promise.all([
        nextEvents.some((event) => event.actor_user_id !== null)
          ? fetchAdminUsers().then((result) => result.items ?? []).catch(() => [])
          : Promise.resolve([]),
        nextEvents.some((event) => event.vault_id !== null)
          ? fetchAdminVaults().then((result) => result.items ?? []).catch(() => [])
          : Promise.resolve([]),
      ]);
      setUsers(nextUsers);
      setVaults(nextVaults);
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
    const user = users.find((candidate) => candidate.id === event.actor_user_id);
    return (
      user?.display_name
      || user?.username
      || t("audit.actor_fallback", { id: event.actor_user_id })
    );
  }

  function resolveVault(event: AuditEvent): string {
    if (event.vault_id === null) return t("audit.no_vault");
    const vault = vaults.find((candidate) => candidate.id === event.vault_id);
    return vault?.name || t("audit.vault_fallback", { id: event.vault_id });
  }

  return (
    <section
      aria-labelledby="admin-audit-events-heading"
      aria-busy={loading}
      className="grid gap-4"
    >
      <div>
        <h2 id="admin-audit-events-heading" className="text-xl font-bold">
          {t("admin.audit_heading")}
        </h2>
        <p className="mt-1 text-sm text-muted">{t("admin.audit_subtitle")}</p>
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
          resolveVault={resolveVault}
          showVault
          idPrefix="admin-audit"
        />
      )}
    </section>
  );
}
