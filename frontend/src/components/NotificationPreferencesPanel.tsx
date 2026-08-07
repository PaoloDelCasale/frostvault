import { useContext, useEffect, useMemo, useState } from "react";
import {
  QueryClientContext,
  useMutation,
  useQuery,
  type QueryClient,
} from "@tanstack/react-query";

import {
  apiQueryKeys,
  createAppQueryClient,
  notificationPreferencesQueryOptions,
  setVaultNotificationPreference,
  type VaultListItem,
  type VaultNotificationPreference,
  type VaultNotificationPreferencePayload,
  type VaultNotificationPreferencesResponse,
} from "@/api";
import { Dialog } from "@/components/Dialog";
import { Button } from "@/components/ui/button";

export type NotificationPreferencesTranslator = (
  key: string,
  params?: Record<string, unknown>,
) => string;

type MutationContext<T> = {
  previous: T | undefined;
};

type PreferenceRow = {
  event: string;
  labelKey: string;
  fallbackLabel: string;
  descriptionKey: string;
  fallbackDescription: string;
};

const PREFERENCE_ROWS: PreferenceRow[] = [
  {
    event: "job_completed",
    labelKey: "ui.notifications_preference_job_completed",
    fallbackLabel: "Completed jobs",
    descriptionKey: "ui.notifications_preference_job_completed_description",
    fallbackDescription: "Get notified when a job completes.",
  },
  {
    event: "job_failed",
    labelKey: "ui.notifications_preference_job_failed",
    fallbackLabel: "Failed jobs",
    descriptionKey: "ui.notifications_preference_job_failed_description",
    fallbackDescription: "Get notified when a job fails.",
  },
];

const PREFERENCE_CHANNELS: Array<{
  channel: "in_app" | "push";
  labelKey: string;
  fallbackLabel: string;
}> = [
  {
    channel: "in_app",
    labelKey: "ui.notifications_channel_in_app",
    fallbackLabel: "In-app",
  },
  {
    channel: "push",
    labelKey: "ui.notifications_channel_push",
    fallbackLabel: "Push",
  },
];

function preferenceLabel(
  t: NotificationPreferencesTranslator | undefined,
  key: string,
  fallback: string,
  params: Record<string, unknown> = {},
): string {
  const translated = t?.(key, params);
  if (translated && translated !== key) return translated;
  return fallback.replace(/\{(\w+)\}/g, (match, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name])
      : match,
  );
}

function preferenceIsEnabled(
  items: VaultNotificationPreference[],
  event: string,
  channel: "in_app" | "push",
): boolean {
  const saved = items.find(
    (item) => item.event === event && item.channel === channel,
  );
  // In-app notices are enabled by default to match the backend. Push keeps its
  // existing enabled-by-default behavior until the user explicitly changes it.
  return saved?.enabled ?? true;
}

export type NotificationPreferencesPanelProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  currentVaultId?: number;
  vaultName?: string;
  vaults?: VaultListItem[];
  onVaultChange?: (vaultId: number) => void;
  locale?: string;
  t?: NotificationPreferencesTranslator;
  /** The app passes its shared client; the fallback keeps shell stories isolated. */
  queryClient?: QueryClient;
};

/**
 * Dedicated personal surface for per-Vault notification delivery preferences.
 * Owned separately from the inbox so opening the bell never loads preferences.
 */
export function NotificationPreferencesPanel({
  open,
  onOpenChange,
  currentVaultId,
  vaultName = "Vault",
  vaults = [],
  onVaultChange,
  locale = "en",
  t,
  queryClient: providedQueryClient,
}: NotificationPreferencesPanelProps) {
  const contextQueryClient = useContext(QueryClientContext);
  const fallbackQueryClient = useMemo(() => createAppQueryClient(), []);
  const queryClient =
    providedQueryClient ?? contextQueryClient ?? fallbackQueryClient;
  const [preferenceError, setPreferenceError] = useState(false);

  const selectedVaultId = currentVaultId ?? 0;
  const preferencesKey = apiQueryKeys.notificationPreferences(selectedVaultId);
  // Resolve the name from the current selection so a stale prop cannot label
  // the wrong Vault after the header switcher changes context.
  const configuredVaultName =
    vaults.find((vault) => vault.id === selectedVaultId)?.name ?? vaultName;

  const preferencesQuery = useQuery(
    {
      ...notificationPreferencesQueryOptions(selectedVaultId),
      enabled: open && selectedVaultId > 0,
      staleTime: 60_000,
    },
    queryClient,
  );

  useEffect(() => {
    if (!open) {
      setPreferenceError(false);
    }
  }, [open]);

  useEffect(() => {
    // Drop save errors when the active Vault changes so stale alerts never
    // describe a previous Vault's failed mutation.
    setPreferenceError(false);
  }, [selectedVaultId]);

  const preferenceMutation = useMutation<
    VaultNotificationPreference,
    Error,
    VaultNotificationPreferencePayload,
    MutationContext<VaultNotificationPreferencesResponse>
  >(
    {
      mutationFn: setVaultNotificationPreference,
      onMutate: async (payload) => {
        await queryClient.cancelQueries({ queryKey: preferencesKey });
        const previous =
          queryClient.getQueryData<VaultNotificationPreferencesResponse>(
            preferencesKey,
          );
        queryClient.setQueryData<
          VaultNotificationPreferencesResponse | undefined
        >(preferencesKey, (current) => {
          const existing = current?.items ?? [];
          const alreadyPresent = existing.some(
            (item) =>
              item.event === payload.event && item.channel === payload.channel,
          );
          const optimistic: VaultNotificationPreference = {
            id: 0,
            user_id: 0,
            vault_id: selectedVaultId,
            event: payload.event,
            channel: payload.channel,
            enabled: payload.enabled,
          };
          return {
            items: alreadyPresent
              ? existing.map((item) =>
                  item.event === payload.event &&
                  item.channel === payload.channel
                    ? { ...item, enabled: payload.enabled }
                    : item,
                )
              : [...existing, optimistic],
          };
        });
        return { previous };
      },
      onError: (_error, _payload, context) => {
        if (context?.previous) {
          queryClient.setQueryData(preferencesKey, context.previous);
        }
        setPreferenceError(true);
      },
      onSuccess: (saved) => {
        queryClient.setQueryData<
          VaultNotificationPreferencesResponse | undefined
        >(preferencesKey, (current) => {
          if (!current) return { items: [saved] };
          const found = current.items.some(
            (item) =>
              item.event === saved.event && item.channel === saved.channel,
          );
          return {
            items: found
              ? current.items.map((item) =>
                  item.event === saved.event && item.channel === saved.channel
                    ? saved
                    : item,
                )
              : [...current.items, saved],
          };
        });
      },
      onSettled: () => {
        if (selectedVaultId > 0) {
          void queryClient.invalidateQueries({
            queryKey: preferencesKey,
            refetchType: "active",
          });
        }
      },
    },
    queryClient,
  );

  const preferenceItems = preferencesQuery.data?.items ?? [];
  const title = preferenceLabel(
    t,
    "ui.notifications_preferences_heading",
    "Notification preferences",
  );
  const description = preferenceLabel(
    t,
    "ui.notifications_preferences_description",
    "Choose how you receive activity for {vault}.",
    { vault: configuredVaultName },
  );
  const vaultFieldLabel = preferenceLabel(t, "ui.vault", "Vault");
  const showVaultSwitcher = vaults.length > 1 && Boolean(onVaultChange);
  // Bind mutations only to the vault_id that owned the loaded preference rows.
  const serverVaultId = preferenceItems.find(
    (item) => typeof item.vault_id === "number" && item.vault_id > 0,
  )?.vault_id;
  const vaultContextMismatch =
    selectedVaultId > 0 &&
    typeof serverVaultId === "number" &&
    serverVaultId !== selectedVaultId;
  const controlsDisabled =
    preferenceMutation.isPending || vaultContextMismatch || selectedVaultId <= 0;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={description}
      closeLabel={preferenceLabel(
        t,
        "ui.notifications_preferences_close",
        "Close notification preferences",
      )}
      className="flex max-h-[calc(100svh-1rem)] w-full flex-col overflow-hidden rounded-t-[18px] rounded-b-none p-4 pb-[max(1rem,env(safe-area-inset-bottom))] top-auto bottom-0 left-0 translate-x-0 translate-y-0 md:top-1/2 md:bottom-auto md:left-1/2 md:w-[min(32rem,calc(100%-30px))] md:-translate-x-1/2 md:-translate-y-1/2 md:rounded-panel md:p-[22px]"
    >
      <section
        aria-label={title}
        className="grid min-h-0 flex-1 gap-4 overflow-y-auto"
        data-testid="notification-preferences"
        data-vault-id={selectedVaultId > 0 ? String(selectedVaultId) : undefined}
      >
        <div className="grid gap-2 rounded-[14px] border border-line bg-canvas p-3">
          <p className="text-sm font-bold text-ink">
            {preferenceLabel(
              t,
              "ui.notifications_preferences_vault_heading",
              "Vault being configured",
            )}
          </p>
          {showVaultSwitcher ? (
            <label className="flex min-h-11 flex-col justify-center gap-1 text-sm font-bold text-muted">
              <span>{vaultFieldLabel}</span>
              <select
                aria-label={vaultFieldLabel}
                className="min-h-11 w-full rounded-[10px] border border-input bg-surface px-3 text-ink"
                value={selectedVaultId > 0 ? selectedVaultId : ""}
                data-testid="notification-preferences-vault"
                onChange={(event) => {
                  const id = Number(event.target.value);
                  if (!Number.isNaN(id) && id > 0) onVaultChange?.(id);
                }}
              >
                {vaults.map((vault) => (
                  <option key={vault.id} value={vault.id}>
                    {vault.name}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <p
              className="text-sm text-muted"
              data-testid="notification-preferences-vault-name"
            >
              {configuredVaultName}
            </p>
          )}
          <p className="text-sm text-muted">
            {preferenceLabel(
              t,
              "ui.notifications_preferences_vault_help",
              "Changes apply only to the selected Vault. Switching Vaults uses the active Vault on the server.",
            )}
          </p>
        </div>

        {preferenceError ? (
          <p role="alert" className="text-sm font-bold text-destructive">
            {preferenceLabel(
              t,
              "ui.notifications_preferences_save_failed",
              "Could not update notification preference.",
            )}
          </p>
        ) : null}

        {vaultContextMismatch ? (
          <p role="alert" className="text-sm font-bold text-destructive">
            {preferenceLabel(
              t,
              "ui.notifications_preferences_vault_mismatch",
              "Vault context changed. Reload preferences before editing.",
            )}
          </p>
        ) : null}

        {selectedVaultId <= 0 ? (
          <p className="text-sm text-muted">
            {preferenceLabel(
              t,
              "ui.notifications_preferences_unavailable",
              "Select a Vault to manage preferences.",
            )}
          </p>
        ) : preferencesQuery.isPending && !preferencesQuery.data ? (
          <p role="status" data-testid="notification-preferences-loading">
            {preferenceLabel(
              t,
              "ui.notifications_preferences_loading",
              "Loading preferences…",
            )}
          </p>
        ) : preferencesQuery.isError && !preferencesQuery.data ? (
          <div
            className="grid gap-3"
            data-testid="notification-preferences-error"
          >
            <p role="alert" className="text-sm font-bold text-destructive">
              {preferenceLabel(
                t,
                "ui.notifications_preferences_error",
                "Unable to load notification preferences.",
              )}
            </p>
            <Button
              type="button"
              variant="secondary"
              className="min-h-11 justify-self-start"
              onClick={() => void preferencesQuery.refetch()}
            >
              {preferenceLabel(
                t,
                "ui.notifications_preferences_retry",
                "Try again",
              )}
            </Button>
          </div>
        ) : (
          <div
            className="grid gap-3"
            role="group"
            aria-label={title}
            data-testid="notification-preferences-controls"
          >
            {PREFERENCE_ROWS.map((row) => {
              const rowLabel = preferenceLabel(
                t,
                row.labelKey,
                row.fallbackLabel,
              );
              return (
                <fieldset
                  key={row.event}
                  className="grid gap-2 rounded-[14px] border border-line bg-canvas p-3"
                >
                  <legend className="px-1 text-sm font-bold text-ink">
                    {rowLabel}
                  </legend>
                  <p className="text-sm text-muted">
                    {preferenceLabel(
                      t,
                      row.descriptionKey,
                      row.fallbackDescription,
                    )}
                  </p>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {PREFERENCE_CHANNELS.map(
                      ({ channel, labelKey, fallbackLabel }) => {
                        const channelLabel = preferenceLabel(
                          t,
                          labelKey,
                          fallbackLabel,
                        );
                        const checked = preferenceIsEnabled(
                          preferenceItems,
                          row.event,
                          channel,
                        );
                        const inputId = `notification-pref-${selectedVaultId}-${row.event}-${channel}`;
                        return (
                          <label
                            key={channel}
                            htmlFor={inputId}
                            className="flex min-h-11 items-center gap-2 rounded-lg border border-line bg-surface px-3 text-sm font-bold text-ink"
                          >
                            <input
                              id={inputId}
                              type="checkbox"
                              checked={checked}
                              disabled={controlsDisabled}
                              aria-label={`${rowLabel}: ${channelLabel}`}
                              onChange={(event) => {
                                if (vaultContextMismatch || selectedVaultId <= 0) {
                                  return;
                                }
                                setPreferenceError(false);
                                preferenceMutation.mutate({
                                  event: row.event,
                                  channel,
                                  enabled: event.target.checked,
                                });
                              }}
                            />
                            {channelLabel}
                          </label>
                        );
                      },
                    )}
                  </div>
                </fieldset>
              );
            })}
          </div>
        )}
      </section>
      {/* locale is accepted for parity with other personal surfaces; dates are unused here. */}
      <span className="sr-only" data-locale={locale}>
        {configuredVaultName}
      </span>
    </Dialog>
  );
}
