import { useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  QueryClientContext,
  useMutation,
  useQuery,
  type QueryClient,
} from "@tanstack/react-query";
import { Bell, Check, CheckCheck } from "lucide-react";

import {
  apiQueryKeys,
  markAllNotificationsRead,
  markNotificationRead,
  notificationPreferencesQueryOptions,
  notificationsQueryOptions,
  setVaultNotificationPreference,
  fetchNotifications,
  type MarkAllNotificationsReadResponse,
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

type UnreadMutationContext = MutationContext<NotificationsResponse> & {
  previousExtra: NotificationItem[];
  previousHasMore: boolean;
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

const UNREAD_PAGE_SIZE = 50;
const READ_PAGE_SIZE = 30;

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

function NotificationRow({
  item,
  locale,
  t,
  markPending,
  onMarkRead,
}: {
  item: NotificationItem;
  locale: string;
  t?: NotificationTranslator;
  markPending: boolean;
  onMarkRead?: (id: number) => void;
}) {
  const title = notificationContent(item.title);
  const body = notificationContent(item.body);
  const unread = !item.read;
  return (
    <li
      className={cn(
        "flex min-h-11 items-start gap-2 border-b border-line py-2 last:border-b-0",
        unread ? "bg-surface" : "bg-canvas/40",
      )}
      data-notification-id={item.id}
      data-read={item.read ? "true" : "false"}
      data-testid="notification-row"
    >
      <span
        aria-hidden="true"
        className={cn(
          "mt-2 size-2.5 shrink-0 rounded-full",
          unread ? "bg-green" : "border border-line bg-transparent",
        )}
        data-testid={unread ? "notification-unread-dot" : "notification-read-dot"}
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <h3 className="min-w-0 flex-1 break-words text-sm font-bold leading-snug text-ink">
            {title}
          </h3>
          <time
            className="shrink-0 pt-0.5 text-xs text-muted"
            dateTime={item.created_at}
          >
            {notificationDate(item.created_at, locale)}
          </time>
        </div>
        {body ? (
          <p className="mt-0.5 line-clamp-2 break-words text-sm leading-snug text-muted">
            {body}
          </p>
        ) : null}
        <span className="sr-only">
          {unread
            ? notificationLabel(t, "ui.notifications_unread", "Unread")
            : notificationLabel(t, "ui.notifications_read", "Read")}
        </span>
      </div>
      {unread && onMarkRead ? (
        <Button
          type="button"
          variant="ghost"
          className="min-h-11 min-w-11 shrink-0 px-2"
          disabled={markPending}
          aria-label={notificationLabel(
            t,
            "ui.notifications_mark_read",
            "Mark as read",
          )}
          onClick={() => onMarkRead(item.id)}
        >
          {markPending ? (
            <span className="sr-only">
              {notificationLabel(
                t,
                "ui.notifications_marking_read",
                "Marking as read…",
              )}
            </span>
          ) : null}
          <Check aria-hidden="true" className="size-4" />
        </Button>
      ) : null}
    </li>
  );
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
  const [markError, setMarkError] = useState<string | null>(null);
  const [preferenceError, setPreferenceError] = useState(false);
  const [showRead, setShowRead] = useState(false);
  const [readItems, setReadItems] = useState<NotificationItem[]>([]);
  const [readHasMore, setReadHasMore] = useState(false);
  const [readLoading, setReadLoading] = useState(false);
  const [readError, setReadError] = useState(false);
  const [readLoadingMore, setReadLoadingMore] = useState(false);
  const [unreadExtra, setUnreadExtra] = useState<NotificationItem[]>([]);
  const [unreadHasMore, setUnreadHasMore] = useState(false);
  const [unreadLoadingMore, setUnreadLoadingMore] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const wasOpenRef = useRef(false);
  const unreadFetchingRef = useRef(false);
  const unreadExtraRef = useRef<NotificationItem[]>([]);
  const unreadHasMoreRef = useRef(false);
  /** Bumped to drop in-flight direct page fetches after list mutations. */
  const unreadLoadGenerationRef = useRef(0);
  const readLoadGenerationRef = useRef(0);

  useEffect(() => {
    unreadExtraRef.current = unreadExtra;
  }, [unreadExtra]);
  useEffect(() => {
    unreadHasMoreRef.current = unreadHasMore;
  }, [unreadHasMore]);

  const selectedVaultId = currentVaultId ?? 0;
  const preferencesKey = apiQueryKeys.notificationPreferences(selectedVaultId);
  const unreadKey = apiQueryKeys.notificationsByStatus("unread");

  const notificationsQuery = useQuery(
    {
      ...notificationsQueryOptions("unread", UNREAD_PAGE_SIZE),
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

  useEffect(() => {
    if (!open) {
      setShowRead(false);
      setReadItems([]);
      setReadHasMore(false);
      setReadError(false);
      setReadLoading(false);
      setReadLoadingMore(false);
      setUnreadExtra([]);
      setUnreadLoadingMore(false);
      unreadLoadGenerationRef.current += 1;
      readLoadGenerationRef.current += 1;
    }
  }, [open]);

  useEffect(() => {
    // After a server refetch settles, resync pagination so appended pages do
    // not drift from the authoritative unread window. Optimistic cache writes
    // do not flip isFetching, so they keep manually loaded rows intact.
    if (notificationsQuery.isFetching) {
      unreadFetchingRef.current = true;
      return;
    }
    if (unreadFetchingRef.current) {
      unreadFetchingRef.current = false;
      setUnreadExtra([]);
    }
    setUnreadHasMore(Boolean(notificationsQuery.data?.has_more));
  }, [notificationsQuery.isFetching, notificationsQuery.data?.has_more]);

  async function loadReadHistory(options?: {
    append?: boolean;
    beforeId?: number;
  }): Promise<void> {
    const append = options?.append === true;
    const generation = ++readLoadGenerationRef.current;
    if (append) {
      setReadLoadingMore(true);
    } else {
      setReadLoading(true);
      setReadError(false);
    }
    try {
      const response = await fetchNotifications({
        status: "read",
        limit: READ_PAGE_SIZE,
        beforeId: options?.beforeId,
      });
      if (generation !== readLoadGenerationRef.current) {
        return;
      }
      setReadItems((current) =>
        append ? [...current, ...response.items] : response.items,
      );
      setReadHasMore(Boolean(response.has_more));
      setReadError(false);
    } catch {
      if (generation !== readLoadGenerationRef.current) {
        return;
      }
      if (!append) {
        setReadItems([]);
        setReadHasMore(false);
      }
      setReadError(true);
    } finally {
      if (generation === readLoadGenerationRef.current) {
        setReadLoading(false);
        setReadLoadingMore(false);
      }
    }
  }

  async function handleToggleReadHistory(): Promise<void> {
    if (showRead) {
      setShowRead(false);
      return;
    }
    setShowRead(true);
    await loadReadHistory();
  }

  const markReadMutation = useMutation<
    NotificationItem,
    Error,
    number,
    UnreadMutationContext
  >(
    {
      mutationFn: markNotificationRead,
      onMutate: async (notificationId) => {
        // Invalidate in-flight Load more so a stale page cannot reintroduce
        // rows the user just marked read.
        unreadLoadGenerationRef.current += 1;
        setUnreadLoadingMore(false);
        await queryClient.cancelQueries({
          queryKey: apiQueryKeys.notifications,
        });
        const previous = queryClient.getQueryData<NotificationsResponse>(unreadKey);
        const previousExtra = unreadExtraRef.current;
        const previousHasMore = unreadHasMoreRef.current;
        queryClient.setQueryData<NotificationsResponse | undefined>(
          unreadKey,
          (current) => {
            if (!current) return current;
            const items = current.items.filter((item) => item.id !== notificationId);
            const removedFromPage = current.items.length !== items.length;
            const removedFromExtra = previousExtra.some(
              (item) => item.id === notificationId,
            );
            return {
              ...current,
              items,
              unread_count:
                removedFromPage || removedFromExtra
                  ? Math.max(0, current.unread_count - 1)
                  : current.unread_count,
            };
          },
        );
        setUnreadExtra((current) =>
          current.filter((item) => item.id !== notificationId),
        );
        return { previous, previousExtra, previousHasMore };
      },
      onError: (_error, _notificationId, context) => {
        if (context?.previous) {
          queryClient.setQueryData(unreadKey, context.previous);
        }
        if (context) {
          setUnreadExtra(context.previousExtra);
          setUnreadHasMore(context.previousHasMore);
        }
        setMarkError(
          notificationLabel(
            t,
            "ui.notifications_mark_read_failed",
            "Could not mark notification as read.",
          ),
        );
      },
      onSuccess: (updated) => {
        if (showRead && updated.read) {
          setReadItems((current) => {
            if (current.some((item) => item.id === updated.id)) {
              return current.map((item) =>
                item.id === updated.id ? updated : item,
              );
            }
            return [updated, ...current];
          });
        }
      },
      onSettled: (_data, error) => {
        // Skip refetch on error so restored extra pages are not wiped by the
        // post-fetch pagination resync effect.
        if (error) return;
        void queryClient.invalidateQueries({
          queryKey: apiQueryKeys.notifications,
          refetchType: "active",
        });
      },
    },
    queryClient,
  );

  const markAllMutation = useMutation<
    MarkAllNotificationsReadResponse,
    Error,
    void,
    UnreadMutationContext
  >(
    {
      mutationFn: markAllNotificationsRead,
      onMutate: async () => {
        unreadLoadGenerationRef.current += 1;
        setUnreadLoadingMore(false);
        await queryClient.cancelQueries({
          queryKey: apiQueryKeys.notifications,
        });
        const previous = queryClient.getQueryData<NotificationsResponse>(unreadKey);
        const previousExtra = unreadExtraRef.current;
        const previousHasMore = unreadHasMoreRef.current;
        queryClient.setQueryData<NotificationsResponse | undefined>(
          unreadKey,
          (current) => {
            if (!current) return current;
            return {
              ...current,
              items: [],
              unread_count: 0,
              has_more: false,
            };
          },
        );
        setUnreadExtra([]);
        setUnreadHasMore(false);
        return { previous, previousExtra, previousHasMore };
      },
      onError: (_error, _variables, context) => {
        if (context?.previous) {
          queryClient.setQueryData(unreadKey, context.previous);
        }
        if (context) {
          setUnreadExtra(context.previousExtra);
          setUnreadHasMore(context.previousHasMore);
        }
        setMarkError(
          notificationLabel(
            t,
            "ui.notifications_mark_all_read_failed",
            "Could not mark all notifications as read.",
          ),
        );
      },
      onSuccess: () => {
        if (showRead) {
          void loadReadHistory();
        }
      },
      onSettled: (_data, error) => {
        if (error) return;
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
  const pageItems = useMemo(
    () => notificationsQuery.data?.items ?? [],
    [notificationsQuery.data?.items],
  );
  const notifications = useMemo(() => {
    if (unreadExtra.length === 0) return pageItems;
    const seen = new Set(pageItems.map((item) => item.id));
    return [
      ...pageItems,
      ...unreadExtra.filter((item) => !seen.has(item.id)),
    ];
  }, [pageItems, unreadExtra]);
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
  const showMarkAll = unreadCount > 0 || notifications.some((item) => !item.read);

  function handleOpenChange(nextOpen: boolean): void {
    setOpen(nextOpen);
    if (nextOpen) {
      setMarkError(null);
      setPreferenceError(false);
    }
  }

  function handleMarkOne(notificationId: number): void {
    setMarkError(null);
    markReadMutation.mutate(notificationId);
  }

  function handleMarkAll(): void {
    setMarkError(null);
    markAllMutation.mutate();
  }

  async function handleLoadMoreUnread(): Promise<void> {
    const last = notifications[notifications.length - 1];
    if (!last) return;
    const generation = ++unreadLoadGenerationRef.current;
    setUnreadLoadingMore(true);
    try {
      const response = await fetchNotifications({
        status: "unread",
        limit: UNREAD_PAGE_SIZE,
        beforeId: last.id,
      });
      if (generation !== unreadLoadGenerationRef.current) {
        return;
      }
      setUnreadExtra((current) => {
        const seen = new Set([
          ...pageItems.map((item) => item.id),
          ...current.map((item) => item.id),
        ]);
        return [
          ...current,
          ...response.items.filter((item) => !seen.has(item.id)),
        ];
      });
      setUnreadHasMore(Boolean(response.has_more));
    } catch {
      if (generation !== unreadLoadGenerationRef.current) {
        return;
      }
      setMarkError(
        notificationLabel(
          t,
          "ui.notifications_error",
          "Unable to load notifications.",
        ),
      );
    } finally {
      if (generation === unreadLoadGenerationRef.current) {
        setUnreadLoadingMore(false);
      }
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
        className="flex max-h-[calc(100svh-1rem)] w-full flex-col overflow-hidden rounded-t-[18px] rounded-b-none p-4 pb-[max(1rem,env(safe-area-inset-bottom))] top-auto bottom-0 left-0 translate-x-0 translate-y-0 md:top-1/2 md:bottom-auto md:left-1/2 md:w-[min(720px,calc(100%-30px))] md:-translate-x-1/2 md:-translate-y-1/2 md:rounded-panel md:p-[22px]"
      >
        <section
          aria-label={notificationTitle}
          className="grid min-h-0 flex-1 gap-4 overflow-y-auto"
          data-testid="notification-center"
        >
          {markError ? (
            <p role="alert" className="text-sm font-bold text-destructive">
              {markError}
            </p>
          ) : null}

          <div className="flex flex-wrap items-center gap-2">
            {showMarkAll ? (
              <Button
                type="button"
                variant="secondary"
                className="min-h-11"
                disabled={markAllMutation.isPending || markReadMutation.isPending}
                data-testid="notifications-mark-all"
                onClick={handleMarkAll}
              >
                <CheckCheck aria-hidden="true" className="size-4" />
                {markAllMutation.isPending
                  ? notificationLabel(
                      t,
                      "ui.notifications_marking_all_read",
                      "Marking all as read…",
                    )
                  : notificationLabel(
                      t,
                      "ui.notifications_mark_all_read",
                      "Mark all as read",
                    )}
              </Button>
            ) : null}
            <Button
              type="button"
              variant="ghost"
              className="min-h-11"
              aria-expanded={showRead}
              data-testid="notifications-toggle-read"
              onClick={() => void handleToggleReadHistory()}
            >
              {showRead
                ? notificationLabel(
                    t,
                    "ui.notifications_hide_read",
                    "Hide read",
                  )
                : notificationLabel(
                    t,
                    "ui.notifications_show_read",
                    "Show read",
                  )}
            </Button>
          </div>

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
                className="min-h-11 justify-self-start"
                onClick={() => void notificationsQuery.refetch()}
              >
                {notificationLabel(t, "ui.notifications_retry", "Try again")}
              </Button>
            </div>
          ) : (
            <div
              className="max-h-[min(50vh,22rem)] overflow-y-auto rounded-[12px] border border-line bg-surface px-2"
              data-testid="notifications-scroll"
            >
              {notifications.length === 0 ? (
                <p
                  role="status"
                  className="p-3 text-sm text-muted"
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
                  className="grid"
                  data-testid="notifications-list"
                >
                  {notifications.map((item) => (
                    <NotificationRow
                      key={item.id}
                      item={item}
                      locale={locale}
                      t={t}
                      markPending={
                        markReadMutation.isPending &&
                        markReadMutation.variables === item.id
                      }
                      onMarkRead={handleMarkOne}
                    />
                  ))}
                </ul>
              )}
              {unreadHasMore ? (
                <div className="p-2">
                  <Button
                    type="button"
                    variant="secondary"
                    className="min-h-11 w-full"
                    disabled={unreadLoadingMore}
                    data-testid="notifications-load-more-unread"
                    onClick={() => void handleLoadMoreUnread()}
                  >
                    {unreadLoadingMore
                      ? notificationLabel(
                          t,
                          "ui.notifications_loading_more",
                          "Loading more…",
                        )
                      : notificationLabel(
                          t,
                          "ui.notifications_load_more",
                          "Load more",
                        )}
                  </Button>
                </div>
              ) : null}
            </div>
          )}

          {showRead ? (
            <section
              aria-labelledby="notification-read-heading"
              className="grid gap-2"
              data-testid="notifications-read-history"
            >
              <h3
                id="notification-read-heading"
                className="text-sm font-bold text-ink"
              >
                {notificationLabel(
                  t,
                  "ui.notifications_read_heading",
                  "Read history",
                )}
              </h3>
              {readLoading ? (
                <p role="status" data-testid="notifications-read-loading">
                  {notificationLabel(
                    t,
                    "ui.notifications_loading",
                    "Loading notifications…",
                  )}
                </p>
              ) : readError ? (
                <div className="grid gap-2" data-testid="notifications-read-error">
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
                    className="min-h-11 justify-self-start"
                    onClick={() => void loadReadHistory()}
                  >
                    {notificationLabel(t, "ui.notifications_retry", "Try again")}
                  </Button>
                </div>
              ) : (
                <div
                  className="max-h-[min(40vh,16rem)] overflow-y-auto rounded-[12px] border border-line bg-canvas px-2"
                  data-testid="notifications-read-scroll"
                >
                  {readItems.length === 0 ? (
                    <p
                      role="status"
                      className="p-3 text-sm text-muted"
                      data-testid="notifications-read-empty"
                    >
                      {notificationLabel(
                        t,
                        "ui.notifications_empty_read",
                        "No read notifications yet.",
                      )}
                    </p>
                  ) : (
                    <ul
                      aria-label={notificationLabel(
                        t,
                        "ui.notifications_read_heading",
                        "Read history",
                      )}
                      className="grid"
                      data-testid="notifications-read-list"
                    >
                      {readItems.map((item) => (
                        <NotificationRow
                          key={item.id}
                          item={item}
                          locale={locale}
                          t={t}
                          markPending={false}
                        />
                      ))}
                    </ul>
                  )}
                  {readHasMore ? (
                    <div className="p-2">
                      <Button
                        type="button"
                        variant="secondary"
                        className="min-h-11 w-full"
                        disabled={readLoadingMore}
                        data-testid="notifications-load-more"
                        onClick={() => {
                          const last = readItems[readItems.length - 1];
                          if (!last) return;
                          void loadReadHistory({
                            append: true,
                            beforeId: last.id,
                          });
                        }}
                      >
                        {readLoadingMore
                          ? notificationLabel(
                              t,
                              "ui.notifications_loading_more",
                              "Loading more…",
                            )
                          : notificationLabel(
                              t,
                              "ui.notifications_load_more",
                              "Load more",
                            )}
                      </Button>
                    </div>
                  ) : null}
                </div>
              )}
            </section>
          ) : null}

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
                  className="min-h-11 justify-self-start"
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
