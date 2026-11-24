import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, type QueryClient } from "@tanstack/react-query";

import {
  fetchNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from "./endpoints";
import { apiQueryKeys, notificationsQueryOptions } from "./query";
import type {
  MarkAllNotificationsReadResponse,
  NotificationItem,
  NotificationsResponse,
} from "./types";

type MutationContext<T> = {
  previous: T | undefined;
};

type UnreadMutationContext = MutationContext<NotificationsResponse> & {
  previousExtra: NotificationItem[];
  previousHasMore: boolean;
};

const UNREAD_PAGE_SIZE = 50;
const READ_PAGE_SIZE = 30;

/** What the list could not do; the caller words it for the Owner. */
export type NotificationFailure = "mark-read" | "mark-all" | "load-more";

/**
 * The notification list behind the bell: the polled unread window, the pages
 * loaded after it, the read history shown on request and marking as read.
 * Closing the list (`open` false) drops every page loaded by hand.
 */
export function useNotificationPaging({
  open,
  queryClient,
  onFailure,
}: {
  open: boolean;
  queryClient: QueryClient;
  onFailure: (failure: NotificationFailure) => void;
}) {
  const [showRead, setShowRead] = useState(false);
  const [readItems, setReadItems] = useState<NotificationItem[]>([]);
  const [readHasMore, setReadHasMore] = useState(false);
  const [readLoading, setReadLoading] = useState(false);
  const [readError, setReadError] = useState(false);
  const [readLoadingMore, setReadLoadingMore] = useState(false);
  const [unreadExtra, setUnreadExtra] = useState<NotificationItem[]>([]);
  const [unreadHasMore, setUnreadHasMore] = useState(false);
  const [unreadLoadingMore, setUnreadLoadingMore] = useState(false);
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

  const unreadKey = apiQueryKeys.notificationsByStatus("unread");

  const notificationsQuery = useQuery(
    {
      ...notificationsQueryOptions("unread", UNREAD_PAGE_SIZE),
      staleTime: 15_000,
      refetchInterval: open ? 15_000 : 60_000,
    },
    queryClient,
  );

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

  async function toggleReadHistory(): Promise<void> {
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
        onFailure("mark-read");
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
        onFailure("mark-all");
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

  const unreadCount =
    typeof notificationsQuery.data?.unread_count === "number" &&
    Number.isFinite(notificationsQuery.data.unread_count)
      ? Math.max(0, notificationsQuery.data.unread_count)
      : 0;
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
  // Serialize mark-one globally and coordinate with mark-all so optimistic
  // snapshots cannot overlap or roll back each other (issue #225 review).
  const markOnePending = markReadMutation.isPending;
  const markAllPending = markAllMutation.isPending;
  const markActionsLocked = markOnePending || markAllPending;
  async function loadMoreUnread(): Promise<void> {
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
      onFailure("load-more");
    } finally {
      if (generation === unreadLoadGenerationRef.current) {
        setUnreadLoadingMore(false);
      }
    }
  }

  return {
    /** The polled first page of unread notifications. */
    query: notificationsQuery,
    /** That page followed by the unread pages loaded after it. */
    notifications,
    unreadCount,
    unreadHasMore,
    unreadLoadingMore,
    loadMoreUnread,
    showRead,
    readItems,
    readHasMore,
    readLoading,
    readError,
    readLoadingMore,
    toggleReadHistory,
    loadReadHistory,
    markOne: (notificationId: number) => markReadMutation.mutate(notificationId),
    /** The notification being marked read, while that request runs. */
    markingId: markOnePending ? markReadMutation.variables : undefined,
    markAll: () => markAllMutation.mutate(),
    markOnePending,
    markAllPending,
    markActionsLocked,
  };
}
