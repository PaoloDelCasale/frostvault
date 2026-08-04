import { useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  QueryClientContext,
  useMutation,
  useQuery,
  type QueryClient,
} from "@tanstack/react-query";
import { Bell, Check } from "lucide-react";

import {
  apiQueryKeys,
  markNotificationRead,
  notificationPreferencesQueryOptions,
  notificationsQueryOptions,
  setVaultNotificationPreference,
  type NotificationItem,
  type NotificationsResponse,
  type VaultNotificationPreference,
  type VaultNotificationPreferencePayload,
  type VaultNotificationPreferencesResponse,
  createAppQueryClient,
} from "@/api";
import { Dialog } from "@/components/Dialog";
import { Button, buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type NotificationTranslator = (
  key: string,
  params?: Record<string, unknown>,
) => string;

type NotificationCenterProps = {
  currentVaultId?: number;
  vaultName?: string;
  locale?: string;
  t?: NotificationTranslator;
  /** The app passes its shared client; the fallback keeps shell stories isolated. */
  queryClient?: QueryClient;
};

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

function notificationLabel(
  t: NotificationTranslator | undefined,
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

function notificationDate(value: string, locale: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  try {
    return new Intl.DateTimeFormat(locale === "it" ? "it-IT" : "en-US", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(date);
  } catch {
    return value;
  }
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

function notificationContent(value: unknown): string {
  return typeof value === "string" ? value : String(value ?? "");
}

export function NotificationCenter({
  currentVaultId,
  vaultName = "Vault",
  locale = "en",
  t,
  queryClient: providedQueryClient,
}: NotificationCenterProps) {
  const contextQueryClient = useContext(QueryClientContext);
  const fallbackQueryClient = useMemo(() => createAppQueryClient(), []);
  const queryClient =
    providedQueryClient ?? contextQueryClient ?? fallbackQueryClient;
  const [open, setOpen] = useState(false);
  const [markError, setMarkError] = useState(false);
  const [preferenceError, setPreferenceError] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const wasOpenRef = useRef(false);

  const selectedVaultId = currentVaultId ?? 0;
  const preferencesKey = apiQueryKeys.notificationPreferences(selectedVaultId);

  const notificationsQuery = useQuery(
    {
      ...notificationsQueryOptions,
      staleTime: 15_000,
    },
    queryClient,
  );
  const preferencesQuery = useQuery(
    {
      ...notificationPreferencesQueryOptions(selectedVaultId),
      enabled: open && selectedVaultId > 0,
      staleTime: 60_000,
    },
    queryClient,
  );

  useEffect(() => {
    if (wasOpenRef.current && !open) {
      // Radix restores focus for Dialog.Trigger users. This controlled dialog
      // is opened by a header button instead, so guarantee the same behavior.
      triggerRef.current?.focus();
    }
    wasOpenRef.current = open;
  }, [open]);

  const markReadMutation = useMutation<
    NotificationItem,
    Error,
    number,
    MutationContext<NotificationsResponse>
  >(
    {
      mutationFn: markNotificationRead,
      onMutate: async (notificationId) => {
        await queryClient.cancelQueries({
          queryKey: apiQueryKeys.notifications,
        });
        const previous = queryClient.getQueryData<NotificationsResponse>(
          apiQueryKeys.notifications,
        );
        const stamp = new Date().toISOString();
        queryClient.setQueryData<NotificationsResponse | undefined>(
          apiQueryKeys.notifications,
          (current) => {
            if (!current) return current;
            let changedUnread = false;
            const items = current.items.map((item) => {
              if (item.id !== notificationId || item.read) return item;
              changedUnread = true;
              return { ...item, read: true, read_at: item.read_at ?? stamp };
            });
            return {
              ...current,
              items,
              unread_count: changedUnread
                ? Math.max(0, current.unread_count - 1)
                : current.unread_count,
            };
          },
        );
        return { previous };
      },
      onError: (_error, _notificationId, context) => {
        if (context?.previous) {
          queryClient.setQueryData(
            apiQueryKeys.notifications,
            context.previous,
          );
        }
        setMarkError(true);
      },
      onSuccess: (updated) => {
        queryClient.setQueryData<NotificationsResponse | undefined>(
          apiQueryKeys.notifications,
          (current) => {
            if (!current) return current;
            return {
              ...current,
              items: current.items.map((item) =>
                item.id === updated.id ? updated : item,
              ),
            };
          },
        );
      },
      onSettled: () => {
        void queryClient.invalidateQueries({
          queryKey: apiQueryKeys.notifications,
          refetchType: "active",
        });
      },
    },
    queryClient,
  );

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
        queryClient.setQueryData<VaultNotificationPreferencesResponse | undefined>(
          preferencesKey,
          (current) => {
            if (!current) return { items: [saved] };
            const found = current.items.some(
              (item) =>
                item.event === saved.event && item.channel === saved.channel,
            );
            return {
              items: found
                ? current.items.map((item) =>
                    item.event === saved.event &&
                    item.channel === saved.channel
                      ? saved
                      : item,
                  )
                : [...current.items, saved],
            };
          },
        );
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

  const unreadCount =
    typeof notificationsQuery.data?.unread_count === "number" &&
    Number.isFinite(notificationsQuery.data.unread_count)
      ? Math.max(0, notificationsQuery.data.unread_count)
      : 0;
  const unreadBadge = unreadCount > 99 ? "99+" : String(unreadCount);
  const bellLabel = notificationLabel(
    t,
    unreadCount > 0
      ? "ui.notifications_open_unread"
      : "ui.notifications_open",
    unreadCount > 0
      ? "Open notifications ({count} unread)"
      : "Open notifications",
    { count: unreadCount },
  );
  const notifications = notificationsQuery.data?.items ?? [];
  const preferenceItems = preferencesQuery.data?.items ?? [];
  const notificationTitle = notificationLabel(
    t,
    "ui.notifications_title",
    "Notifications",
  );
  const notificationDescription = notificationLabel(
    t,
    "ui.notifications_description",
    "Recent activity for your Vault.",
  );

  function handleOpenChange(nextOpen: boolean): void {
    setOpen(nextOpen);
    if (nextOpen) {
      setMarkError(false);
      setPreferenceError(false);
    }
  }

  return (
    <>
      <div className="relative">
        <button
          ref={triggerRef}
          type="button"
          className={buttonVariants({ variant: "secondary", size: "icon" })}
          aria-label={bellLabel}
          aria-haspopup="dialog"
          aria-expanded={open}
          data-testid="notification-bell"
          onClick={() => handleOpenChange(true)}
        >
          <Bell className="size-5" aria-hidden="true" />
          {unreadCount > 0 ? (
            <span
              aria-hidden="true"
              className="absolute -top-1 -right-1 grid min-h-5 min-w-5 place-items-center rounded-full bg-destructive px-1 text-[11px] font-extrabold text-white"
              data-testid="notification-unread-badge"
            >
              {unreadBadge}
            </span>
          ) : null}
        </button>
      </div>

      <Dialog
        open={open}
        onOpenChange={handleOpenChange}
        title={notificationTitle}
        description={notificationDescription}
        closeLabel={notificationLabel(
          t,
          "ui.notifications_close",
          "Close notifications",
        )}
        className="max-h-[calc(100svh-1rem)] overflow-y-auto rounded-t-[18px] rounded-b-none p-4 pb-[max(1rem,env(safe-area-inset-bottom))] top-auto bottom-0 left-0 w-full translate-x-0 translate-y-0 md:top-1/2 md:bottom-auto md:left-1/2 md:w-[min(720px,calc(100%-30px))] md:-translate-x-1/2 md:-translate-y-1/2 md:rounded-panel md:p-[22px]"
      >
        <section
          aria-label={notificationTitle}
          className="grid gap-5"
          data-testid="notification-center"
        >
          {markError ? (
            <p role="alert" className="text-sm font-bold text-destructive">
              {notificationLabel(
                t,
                "ui.notifications_mark_read_failed",
                "Could not mark notification as read.",
              )}
            </p>
          ) : null}

          {notificationsQuery.isPending && !notificationsQuery.data ? (
            <p role="status" aria-live="polite" data-testid="notifications-loading">
              {notificationLabel(t, "ui.notifications_loading", "Loading notifications…")}
            </p>
          ) : notificationsQuery.isError && !notificationsQuery.data ? (
            <div className="grid gap-3" data-testid="notifications-error">
              <p role="alert" className="text-sm font-bold text-destructive">
                {notificationLabel(
                  t,
                  "ui.notifications_error",
                  "Unable to load notifications.",
                )}
              </p>
              <Button
                type="button"
                variant="secondary"
                className="justify-self-start"
                onClick={() => void notificationsQuery.refetch()}
              >
                {notificationLabel(t, "ui.notifications_retry", "Try again")}
              </Button>
            </div>
          ) : notifications.length === 0 ? (
            <p
              role="status"
              className="rounded-[14px] border border-line bg-canvas p-4 text-sm text-muted"
              data-testid="notifications-empty"
            >
              {notificationLabel(
                t,
                "ui.notifications_empty",
                "You're all caught up.",
              )}
            </p>
          ) : (
            <ul
              aria-label={notificationTitle}
              className="grid gap-3"
              data-testid="notifications-list"
            >
              {notifications.map((item) => {
                const title = notificationContent(item.title);
                const body = notificationContent(item.body);
                const markingThis =
                  markReadMutation.isPending &&
                  markReadMutation.variables === item.id;
                return (
                  <li
                    key={item.id}
                    className={cn(
                      "grid gap-3 rounded-[14px] border border-line bg-surface p-3",
                      item.read ? "opacity-75" : "border-green/50",
                    )}
                    data-notification-id={item.id}
                    data-read={item.read ? "true" : "false"}
                  >
                    <div className="flex min-w-0 items-start gap-3">
                      <span
                        aria-hidden="true"
                        className={cn(
                          "mt-1 size-2.5 shrink-0 rounded-full",
                          item.read ? "bg-line" : "bg-green",
                        )}
                      />
                      <div className="min-w-0 flex-1">
                        <h3 className="break-words text-sm font-bold text-ink">
                          {title}
                        </h3>
                        {body ? (
                          <p className="mt-1 break-words text-sm leading-relaxed text-muted">
                            {body}
                          </p>
                        ) : null}
                        <time
                          className="mt-2 block text-xs text-muted"
                          dateTime={item.created_at}
                        >
                          {notificationDate(item.created_at, locale)}
                        </time>
                      </div>
                    </div>
                    <div className="flex items-center justify-between gap-3 border-t border-line pt-2">
                      <span className="text-xs font-bold text-muted">
                        {item.read
                          ? notificationLabel(t, "ui.notifications_read", "Read")
                          : notificationLabel(
                              t,
                              "ui.notifications_unread",
                              "Unread",
                            )}
                      </span>
                      {!item.read ? (
                        <Button
                          type="button"
                          variant="ghost"
                          className="min-h-11 px-2 text-sm"
                          disabled={markReadMutation.isPending}
                          aria-label={notificationLabel(
                            t,
                            "ui.notifications_mark_read",
                            "Mark as read",
                          )}
                          onClick={() => {
                            setMarkError(false);
                            markReadMutation.mutate(item.id);
                          }}
                        >
                          {markingThis ? (
                            notificationLabel(
                              t,
                              "ui.notifications_marking_read",
                              "Marking as read…",
                            )
                          ) : (
                            <>
                              <Check aria-hidden="true" />
                              {notificationLabel(
                                t,
                                "ui.notifications_mark_read",
                                "Mark as read",
                              )}
                            </>
                          )}
                        </Button>
                      ) : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}

          <section
            aria-labelledby="notification-preferences-heading"
            className="grid gap-3 border-t border-line pt-4"
            data-testid="notification-preferences"
          >
            <div>
              <h3
                id="notification-preferences-heading"
                className="text-base font-bold text-ink"
              >
                {notificationLabel(
                  t,
                  "ui.notifications_preferences_heading",
                  "Notification preferences",
                )}
              </h3>
              <p className="mt-1 text-sm text-muted">
                {notificationLabel(
                  t,
                  "ui.notifications_preferences_description",
                  "Choose how you receive activity for {vault}.",
                  { vault: vaultName },
                )}
              </p>
            </div>

            {preferenceError ? (
              <p role="alert" className="text-sm font-bold text-destructive">
                {notificationLabel(
                  t,
                  "ui.notifications_preferences_save_failed",
                  "Could not update notification preference.",
                )}
              </p>
            ) : null}

            {selectedVaultId <= 0 ? (
              <p className="text-sm text-muted">
                {notificationLabel(
                  t,
                  "ui.notifications_preferences_unavailable",
                  "Select a Vault to manage preferences.",
                )}
              </p>
            ) : preferencesQuery.isPending && !preferencesQuery.data ? (
              <p role="status" data-testid="notification-preferences-loading">
                {notificationLabel(
                  t,
                  "ui.notifications_preferences_loading",
                  "Loading preferences…",
                )}
              </p>
            ) : preferencesQuery.isError && !preferencesQuery.data ? (
              <div className="grid gap-3" data-testid="notification-preferences-error">
                <p role="alert" className="text-sm font-bold text-destructive">
                  {notificationLabel(
                    t,
                    "ui.notifications_preferences_error",
                    "Unable to load notification preferences.",
                  )}
                </p>
                <Button
                  type="button"
                  variant="secondary"
                  className="justify-self-start"
                  onClick={() => void preferencesQuery.refetch()}
                >
                  {notificationLabel(
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
                aria-label={notificationLabel(
                  t,
                  "ui.notifications_preferences_heading",
                  "Notification preferences",
                )}
              >
                {PREFERENCE_ROWS.map((row) => {
                  const rowLabel = notificationLabel(
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
                        {notificationLabel(
                          t,
                          row.descriptionKey,
                          row.fallbackDescription,
                        )}
                      </p>
                      <div className="grid gap-2 sm:grid-cols-2">
                        {PREFERENCE_CHANNELS.map(({ channel, labelKey, fallbackLabel }) => {
                          const channelLabel = notificationLabel(
                            t,
                            labelKey,
                            fallbackLabel,
                          );
                          const checked = preferenceIsEnabled(
                            preferenceItems,
                            row.event,
                            channel,
                          );
                          const inputId = `notification-${row.event}-${channel}`;
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
                                disabled={preferenceMutation.isPending}
                                aria-label={`${rowLabel}: ${channelLabel}`}
                                onChange={(event) => {
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
                        })}
                      </div>
                    </fieldset>
                  );
                })}
              </div>
            )}
          </section>
        </section>
      </Dialog>
    </>
  );
}
