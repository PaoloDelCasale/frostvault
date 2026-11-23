import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { QueryClientContext, type QueryClient } from "@tanstack/react-query";
import { Bell, Check, CheckCheck } from "lucide-react";

import { createAppQueryClient, useNotificationPaging, type NotificationItem } from "@/api";
import { Dialog } from "@/components/Dialog";
import { Button, buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { problemLink } from "@/lib/problemLink";

export type NotificationTranslator = (
  key: string,
  params?: Record<string, unknown>,
) => string;

type NotificationCenterProps = {
  locale?: string;
  t?: NotificationTranslator;
  /** The app passes its shared client; the fallback keeps shell stories isolated. */
  queryClient?: QueryClient;
};

/** Bodies longer than this may overflow the two-line clamp. */
const NOTIFICATION_CLAMP_CHARS = 120;

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

function notificationContent(value: unknown): string {
  return typeof value === "string" ? value : String(value ?? "");
}

function NotificationRow({
  item,
  locale,
  t,
  markPending,
  markDisabled,
  onMarkRead,
}: {
  item: NotificationItem;
  locale: string;
  t?: NotificationTranslator;
  /** True only for the in-flight row (screen-reader progress). */
  markPending: boolean;
  /** True for every row while any mark-one/mark-all mutation is open. */
  markDisabled: boolean;
  onMarkRead?: (id: number) => void;
}) {
  const title = notificationContent(item.title);
  const body = notificationContent(item.body);
  const unread = !item.read && item.problem_status !== "resolved";
  const [expanded, setExpanded] = useState(false);
  // Long remedies are clamped to two lines; the toggle reveals the full text
  // even for notifications without a Vault page to open (#657).
  const expandable = item.event !== "weekly_summary" && (body.length > NOTIFICATION_CLAMP_CHARS || body.includes("\n"));
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
            {item.vault_id || item.event === "weekly_summary" ? <a className="underline" href={problemLink(item.vault_id, item.message_params)}>{title}</a> : title}
          </h3>
          <time
            className="shrink-0 pt-0.5 text-xs text-muted"
            dateTime={item.created_at}
          >
            {notificationDate(item.created_at, locale)}
          </time>
        </div>
        {body ? (
          <p id={`notification-body-${item.id}`} className={cn("mt-0.5 whitespace-pre-line break-words text-sm leading-snug text-muted", expandable && !expanded && "line-clamp-2")} data-testid="notification-body">
            {body}
          </p>
        ) : null}
        {expandable ? (
          <button
            type="button"
            className="min-h-11 text-xs font-bold text-ink underline"
            aria-expanded={expanded}
            aria-controls={`notification-body-${item.id}`}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded
              ? notificationLabel(t, "ui.notifications_show_less", "Show less")
              : notificationLabel(t, "ui.notifications_show_more", "Show all")}
          </button>
        ) : null}
        {item.problem_status ? (
          <p className="text-xs font-bold" data-testid="notification-problem-status">
            {item.problem_status === "open"
              ? notificationLabel(t, "ui.problem_open", "Open problem")
              : notificationLabel(t, "ui.problem_resolved", "Resolved problem")}
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
          disabled={markDisabled}
          aria-busy={markPending || undefined}
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
  const triggerRef = useRef<HTMLButtonElement>(null);
  const wasOpenRef = useRef(false);
  useEffect(() => {
    if (wasOpenRef.current && !open) {
      // Radix restores focus for Dialog.Trigger users. This controlled dialog
      // is opened by a header button instead, so guarantee the same behavior.
      triggerRef.current?.focus();
    }
    wasOpenRef.current = open;
  }, [open]);

  const {
    query: notificationsQuery, notifications, unreadCount, unreadHasMore, unreadLoadingMore, loadMoreUnread,
    showRead, readItems, readHasMore, readLoading, readError, readLoadingMore, toggleReadHistory, loadReadHistory,
    markOne, markingId, markAll, markAllPending, markActionsLocked,
  } = useNotificationPaging({
    open,
    queryClient,
    onFailure: (failure) => setMarkError(
      failure === "mark-read" ? notificationLabel(t, "ui.notifications_mark_read_failed", "Could not mark notification as read.")
        : failure === "mark-all" ? notificationLabel(t, "ui.notifications_mark_all_read_failed", "Could not mark all notifications as read.")
          : notificationLabel(t, "ui.notifications_error", "Unable to load notifications."),
    ),
  });
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
  // Keep the bulk control mounted while its mutation is in flight so the
  // busy/disabled state remains exposed after the optimistic empty list.
  const showMarkAll =
    markAllPending ||
    unreadCount > 0 ||
    notifications.some((item) => !item.read);

  function handleOpenChange(nextOpen: boolean): void {
    setOpen(nextOpen);
    if (nextOpen) {
      setMarkError(null);
      if (notificationsQuery.data && notificationsQuery.isStale) {
        void notificationsQuery.refetch();
      }
    }
  }

  function handleMarkOne(notificationId: number): void {
    if (markActionsLocked) return;
    setMarkError(null);
    markOne(notificationId);
  }

  function handleMarkAll(): void {
    if (markActionsLocked) return;
    setMarkError(null);
    markAll();
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
              className="absolute -top-1 -right-1 grid min-h-5 min-w-5 place-items-center rounded-full bg-destructive px-1 text-[11px] font-extrabold text-destructive-foreground"
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
          <p data-testid="notification-open-problem-count" className="text-sm font-bold">
            {notificationLabel(t, "ui.open_problems_count", "{count} open problems", {
              count: notificationsQuery.data?.open_problem_count ?? 0,
            })}
          </p>
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
                disabled={markActionsLocked}
                aria-busy={markAllPending || undefined}
                data-testid="notifications-mark-all"
                onClick={handleMarkAll}
              >
                <CheckCheck aria-hidden="true" className="size-4" />
                {markAllPending
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
              onClick={() => void toggleReadHistory()}
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
                        markingId === item.id
                      }
                      markDisabled={markActionsLocked}
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
                    onClick={() => void loadMoreUnread()}
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
                          markDisabled
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
        </section>
      </Dialog>
    </>
  );
}
