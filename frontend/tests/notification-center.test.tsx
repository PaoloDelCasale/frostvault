import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
  type ApiFetch,
} from "@/api";
import { NotificationCenter } from "@/components/NotificationCenter";

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const catalog: Record<string, string> = {
  "ui.notifications_open": "Open notifications",
  "ui.notifications_open_unread": "Open notifications ({count} unread)",
  "ui.notifications_title": "Notifications",
  "ui.notifications_description": "Recent activity for your Vault.",
  "ui.notifications_close": "Close notifications",
  "ui.notifications_loading": "Loading notifications…",
  "ui.notifications_empty": "You're all caught up.",
  "ui.notifications_empty_read": "No read notifications yet.",
  "ui.notifications_read": "Read",
  "ui.notifications_unread": "Unread",
  "ui.notifications_mark_read": "Mark as read",
  "ui.notifications_marking_read": "Marking as read…",
  "ui.notifications_mark_read_failed": "Could not mark notification as read.",
  "ui.notifications_mark_all_read": "Mark all as read",
  "ui.notifications_marking_all_read": "Marking all as read…",
  "ui.notifications_mark_all_read_failed":
    "Could not mark all notifications as read.",
  "ui.notifications_show_read": "Show read",
  "ui.notifications_hide_read": "Hide read",
  "ui.notifications_read_heading": "Read history",
  "ui.notifications_load_more": "Load more",
  "ui.notifications_loading_more": "Loading more…",
  "ui.notifications_preferences_heading": "Notification preferences",
  "ui.notifications_preferences_description":
    "Choose how you receive activity for {vault}.",
  "ui.notifications_channel_in_app": "In-app",
  "ui.notifications_channel_push": "Push",
  "ui.notifications_preference_job_completed": "Completed jobs",
  "ui.notifications_preference_job_completed_description":
    "Get notified when a job completes.",
  "ui.notifications_preference_job_failed": "Failed jobs",
  "ui.notifications_preference_job_failed_description":
    "Get notified when a job fails.",
};

function translate(key: string, params: Record<string, unknown> = {}): string {
  return (catalog[key] ?? key).replace(/\{(\w+)\}/g, (match, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name])
      : match,
  );
}

function notificationItem(
  overrides: Partial<{
    id: number;
    title: string;
    body: string;
    read: boolean;
    created_at: string;
  }> = {},
) {
  const id = overrides.id ?? 10;
  const read = overrides.read ?? false;
  return {
    id,
    user_id: 7,
    vault_id: 9,
    job_id: 44,
    event: "job_completed",
    title: overrides.title ?? `Notification ${id}`,
    body: overrides.body ?? `Body ${id}`,
    title_key: null,
    body_key: null,
    message_params: {},
    in_app_enabled: true,
    dedupe_key: `job:${id}:job_completed`,
    created_at: overrides.created_at ?? "2025-01-01T10:00:00Z",
    read,
    read_at: read ? "2025-01-01T10:01:00Z" : null,
  };
}

function renderCenter(fetchMock: ApiFetch) {
  configureApiClient({ fetch: fetchMock, csrfToken: "notification-csrf" });
  return render(
    <NotificationCenter
      currentVaultId={9}
      vaultName="Test Archive"
      locale="en"
      t={translate}
      queryClient={createAppQueryClient()}
    />,
  );
}

function requestUrl(call: unknown[]): string {
  return String(call[0] ?? "");
}

describe("NotificationCenter", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  afterEach(() => {
    resetApiClientForTests();
  });

  it("defaults to unread-only inbox, hides read history, and removes a marked item", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        jsonResponse({
          unread_count: 1,
          has_more: false,
          items: [
            notificationItem({
              id: 10,
              title: "<img src=x onerror=alert(1)>",
              body: "<script>alert(1)</script>",
            }),
          ],
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(
        jsonResponse(
          notificationItem({
            id: 10,
            title: "<img src=x onerror=alert(1)>",
            body: "<script>alert(1)</script>",
            read: true,
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({ unread_count: 0, has_more: false, items: [] }),
      );

    renderCenter(fetchMock);
    const bell = await screen.findByRole("button", {
      name: "Open notifications (1 unread)",
    });
    expect(screen.getByTestId("notification-unread-badge")).toHaveTextContent("1");

    await user.click(bell);
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    expect(dialog).toHaveTextContent("<img src=x onerror=alert(1)>");
    expect(dialog).toHaveTextContent("<script>alert(1)</script>");
    expect(dialog.querySelector("script")).toBeNull();
    expect(dialog.querySelector("img")).toBeNull();
    expect(screen.queryByTestId("notifications-read-history")).toBeNull();
    expect(
      fetchMock.mock.calls.map((call) => requestUrl(call)).some((url) =>
        url.includes("status=unread"),
      ),
    ).toBe(true);

    await user.click(
      within(dialog).getByRole("button", { name: "Mark as read" }),
    );
    await waitFor(() => {
      expect(screen.queryByTestId("notification-unread-badge")).not.toBeInTheDocument();
    });
    expect(fetchMock.mock.calls.map((call) => requestUrl(call))).toContain(
      "/api/notifications/read",
    );
    expect(within(dialog).queryByTestId("notification-row")).toBeNull();
    expect(within(dialog).getByTestId("notifications-empty")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(bell).toHaveFocus();
  });

  it("discloses read history on demand and keeps it out of the default list", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/notifications?") && url.includes("status=unread")) {
        return jsonResponse({
          unread_count: 1,
          has_more: false,
          items: [notificationItem({ id: 20, title: "Unread only" })],
        });
      }
      if (url.startsWith("/api/notifications?") && url.includes("status=read")) {
        return jsonResponse({
          unread_count: 1,
          has_more: false,
          items: [
            notificationItem({
              id: 11,
              title: "Already read",
              read: true,
            }),
          ],
        });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      throw new Error(`unexpected ${url}`);
    });

    renderCenter(fetchMock);
    await user.click(
      await screen.findByRole("button", {
        name: "Open notifications (1 unread)",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    expect(within(dialog).getByText("Unread only")).toBeInTheDocument();
    expect(within(dialog).queryByText("Already read")).toBeNull();

    await user.click(
      within(dialog).getByRole("button", { name: "Show read" }),
    );
    expect(
      await within(dialog).findByTestId("notifications-read-history"),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("Already read")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.map((call) => requestUrl(call)).some((url) =>
        url.includes("status=read"),
      ),
    ).toBe(true);

    await user.click(
      within(dialog).getByRole("button", { name: "Hide read" }),
    );
    expect(within(dialog).queryByTestId("notifications-read-history")).toBeNull();
    expect(within(dialog).queryByText("Already read")).toBeNull();
  });

  it("marks all unread optimistically and rolls back on failure", async () => {
    const user = userEvent.setup();
    let markAllCalls = 0;
    let allRead = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/notifications?") && url.includes("status=unread")) {
        if (allRead) {
          return jsonResponse({
            unread_count: 0,
            has_more: false,
            items: [],
          });
        }
        return jsonResponse({
          unread_count: 2,
          has_more: false,
          items: [
            notificationItem({ id: 1, title: "First" }),
            notificationItem({ id: 2, title: "Second" }),
          ],
        });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      if (url === "/api/notifications/read-all" && init?.method === "POST") {
        markAllCalls += 1;
        if (markAllCalls === 1) {
          return new Response("nope", { status: 500 });
        }
        allRead = true;
        return jsonResponse({ marked_count: 2, unread_count: 0 });
      }
      throw new Error(`unexpected ${url}`);
    });

    renderCenter(fetchMock);
    await user.click(
      await screen.findByRole("button", {
        name: "Open notifications (2 unread)",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    const markAll = within(dialog).getByRole("button", {
      name: "Mark all as read",
    });

    await user.click(markAll);
    expect(
      await within(dialog).findByRole("alert"),
    ).toHaveTextContent("Could not mark all notifications as read.");
    expect(within(dialog).getByText("First")).toBeInTheDocument();
    expect(within(dialog).getByText("Second")).toBeInTheDocument();
    expect(screen.getByTestId("notification-unread-badge")).toHaveTextContent("2");

    await user.click(markAll);
    await waitFor(() => {
      expect(screen.queryByTestId("notification-unread-badge")).not.toBeInTheDocument();
    });
    expect(within(dialog).getByTestId("notifications-empty")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Mark all as read" })).toBeNull();
  });

  it("disables other mark-one and mark-all actions while one mark-one is pending, then rolls back coherently", async () => {
    const user = userEvent.setup();
    const markGate: { release: ((response: Response) => void) | null } = {
      release: null,
    };
    let markCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/notifications?") && url.includes("status=unread")) {
        return jsonResponse({
          unread_count: 2,
          has_more: false,
          items: [
            notificationItem({ id: 1, title: "Alpha unread" }),
            notificationItem({ id: 2, title: "Beta unread" }),
          ],
        });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      if (url === "/api/notifications/read" && init?.method === "POST") {
        markCalls += 1;
        return await new Promise<Response>((resolve) => {
          markGate.release = resolve;
        });
      }
      if (url === "/api/notifications/read-all" && init?.method === "POST") {
        throw new Error("mark-all must not run while mark-one is pending");
      }
      throw new Error(`unexpected ${url}`);
    });

    renderCenter(fetchMock);
    await user.click(
      await screen.findByRole("button", {
        name: "Open notifications (2 unread)",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    const markButtons = within(dialog).getAllByRole("button", {
      name: "Mark as read",
    });
    expect(markButtons).toHaveLength(2);

    await user.click(markButtons[0]!);
    await waitFor(() => {
      expect(markCalls).toBe(1);
      expect(markGate.release).not.toBeNull();
    });

    // Optimistic mark-one removes Alpha; Beta remains locked until A settles.
    expect(within(dialog).queryByText("Alpha unread")).toBeNull();
    expect(within(dialog).getByText("Beta unread")).toBeInTheDocument();
    const betaMark = within(dialog).getByRole("button", { name: "Mark as read" });
    expect(betaMark).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "Mark all as read" }),
    ).toBeDisabled();

    // A second click must not enqueue another mark-one while the first is open.
    await user.click(betaMark);
    expect(markCalls).toBe(1);

    markGate.release?.(new Response("nope", { status: 500 }));

    expect(
      await within(dialog).findByRole("alert"),
    ).toHaveTextContent("Could not mark notification as read.");
    expect(within(dialog).getByText("Alpha unread")).toBeInTheDocument();
    expect(within(dialog).getByText("Beta unread")).toBeInTheDocument();
    expect(screen.getByTestId("notification-unread-badge")).toHaveTextContent("2");

    const recovered = within(dialog).getAllByRole("button", {
      name: "Mark as read",
    });
    expect(recovered).toHaveLength(2);
    for (const button of recovered) {
      expect(button).toBeEnabled();
    }
    expect(
      within(dialog).getByRole("button", { name: "Mark all as read" }),
    ).toBeEnabled();
    expect(markCalls).toBe(1);
  });

  it("disables individual mark-one actions while mark-all is pending", async () => {
    const user = userEvent.setup();
    const markAllGate: { release: ((response: Response) => void) | null } = {
      release: null,
    };
    let markOneCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/notifications?") && url.includes("status=unread")) {
        return jsonResponse({
          unread_count: 2,
          has_more: false,
          items: [
            notificationItem({ id: 1, title: "One" }),
            notificationItem({ id: 2, title: "Two" }),
          ],
        });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      if (url === "/api/notifications/read" && init?.method === "POST") {
        markOneCalls += 1;
        return jsonResponse(notificationItem({ id: 1, read: true }));
      }
      if (url === "/api/notifications/read-all" && init?.method === "POST") {
        return await new Promise<Response>((resolve) => {
          markAllGate.release = resolve;
        });
      }
      throw new Error(`unexpected ${url}`);
    });

    renderCenter(fetchMock);
    await user.click(
      await screen.findByRole("button", {
        name: "Open notifications (2 unread)",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    await user.click(
      within(dialog).getByRole("button", { name: "Mark all as read" }),
    );
    await waitFor(() => {
      expect(markAllGate.release).not.toBeNull();
    });

    const markButtons = within(dialog).queryAllByRole("button", {
      name: "Mark as read",
    });
    // Optimistic mark-all clears unread rows, so individual actions disappear.
    expect(markButtons).toHaveLength(0);
    expect(
      within(dialog).getByRole("button", { name: /Marking all as read/i }),
    ).toBeDisabled();

    markAllGate.release?.(new Response("nope", { status: 500 }));
    expect(
      await within(dialog).findByRole("alert"),
    ).toHaveTextContent("Could not mark all notifications as read.");
    expect(markOneCalls).toBe(0);
    const recovered = within(dialog).getAllByRole("button", {
      name: "Mark as read",
    });
    expect(recovered).toHaveLength(2);
    for (const button of recovered) {
      expect(button).toBeEnabled();
    }
  });

  it("rolls back a failed single mark-read and keeps the row accessible", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/notifications?") && url.includes("status=unread")) {
        return jsonResponse({
          unread_count: 1,
          has_more: false,
          items: [notificationItem({ id: 44, title: "Sticky unread" })],
        });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      if (url === "/api/notifications/read" && init?.method === "POST") {
        return new Response("nope", { status: 500 });
      }
      throw new Error(`unexpected ${url}`);
    });

    renderCenter(fetchMock);
    await user.click(
      await screen.findByRole("button", {
        name: "Open notifications (1 unread)",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    await user.click(
      within(dialog).getByRole("button", { name: "Mark as read" }),
    );
    expect(
      await within(dialog).findByRole("alert"),
    ).toHaveTextContent("Could not mark notification as read.");
    expect(within(dialog).getByText("Sticky unread")).toBeInTheDocument();
    expect(screen.getByTestId("notification-unread-badge")).toHaveTextContent("1");
  });

  it("restores loaded extra unread pages when mark-one fails after Load more", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/notifications?") && url.includes("status=unread")) {
        if (url.includes("before_id=")) {
          return jsonResponse({
            unread_count: 2,
            has_more: false,
            items: [notificationItem({ id: 1, title: "Older unread" })],
          });
        }
        return jsonResponse({
          unread_count: 2,
          has_more: true,
          items: [notificationItem({ id: 2, title: "Newer unread" })],
        });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      if (url === "/api/notifications/read" && init?.method === "POST") {
        return new Response("nope", { status: 500 });
      }
      throw new Error(`unexpected ${url}`);
    });

    renderCenter(fetchMock);
    await user.click(
      await screen.findByRole("button", {
        name: "Open notifications (2 unread)",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    expect(within(dialog).getByText("Newer unread")).toBeInTheDocument();
    expect(within(dialog).queryByText("Older unread")).toBeNull();

    await user.click(
      within(dialog).getByRole("button", { name: "Load more" }),
    );
    expect(await within(dialog).findByText("Older unread")).toBeInTheDocument();

    const olderRow = within(dialog)
      .getByText("Older unread")
      .closest("[data-testid='notification-row']");
    expect(olderRow).not.toBeNull();
    await user.click(
      within(olderRow as HTMLElement).getByRole("button", { name: "Mark as read" }),
    );

    expect(
      await within(dialog).findByRole("alert"),
    ).toHaveTextContent("Could not mark notification as read.");
    expect(within(dialog).getByText("Newer unread")).toBeInTheDocument();
    expect(within(dialog).getByText("Older unread")).toBeInTheDocument();
    expect(screen.getByTestId("notification-unread-badge")).toHaveTextContent("2");
    expect(
      within(dialog).queryByTestId("notifications-load-more-unread"),
    ).not.toBeInTheDocument();
  });

  it("restores loaded extra unread pages when mark-all fails after Load more", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/notifications?") && url.includes("status=unread")) {
        if (url.includes("before_id=")) {
          return jsonResponse({
            unread_count: 2,
            has_more: true,
            items: [notificationItem({ id: 1, title: "Page two" })],
          });
        }
        return jsonResponse({
          unread_count: 2,
          has_more: true,
          items: [notificationItem({ id: 2, title: "Page one" })],
        });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      if (url === "/api/notifications/read-all" && init?.method === "POST") {
        return new Response("nope", { status: 500 });
      }
      throw new Error(`unexpected ${url}`);
    });

    renderCenter(fetchMock);
    await user.click(
      await screen.findByRole("button", {
        name: "Open notifications (2 unread)",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    await user.click(
      within(dialog).getByRole("button", { name: "Load more" }),
    );
    expect(await within(dialog).findByText("Page two")).toBeInTheDocument();

    await user.click(
      within(dialog).getByRole("button", { name: "Mark all as read" }),
    );
    expect(
      await within(dialog).findByRole("alert"),
    ).toHaveTextContent("Could not mark all notifications as read.");
    expect(within(dialog).getByText("Page one")).toBeInTheDocument();
    expect(within(dialog).getByText("Page two")).toBeInTheDocument();
    expect(screen.getByTestId("notification-unread-badge")).toHaveTextContent("2");
    expect(
      within(dialog).getByTestId("notifications-load-more-unread"),
    ).toBeInTheDocument();
  });

  it("ignores a stale Load more response after mark-all empties the unread list", async () => {
    const user = userEvent.setup();
    const loadMoreGate: {
      release: ((response: Response) => void) | null;
    } = { release: null };
    let allRead = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/notifications?") && url.includes("status=unread")) {
        if (url.includes("before_id=")) {
          return await new Promise<Response>((resolve) => {
            loadMoreGate.release = resolve;
          });
        }
        if (allRead) {
          return jsonResponse({ unread_count: 0, has_more: false, items: [] });
        }
        return jsonResponse({
          unread_count: 2,
          has_more: true,
          items: [notificationItem({ id: 20, title: "Visible first page" })],
        });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      if (url === "/api/notifications/read-all" && init?.method === "POST") {
        allRead = true;
        return jsonResponse({ marked_count: 2, unread_count: 0 });
      }
      throw new Error(`unexpected ${url}`);
    });

    renderCenter(fetchMock);
    await user.click(
      await screen.findByRole("button", {
        name: "Open notifications (2 unread)",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    await user.click(
      within(dialog).getByRole("button", { name: "Load more" }),
    );
    await waitFor(() => {
      expect(loadMoreGate.release).not.toBeNull();
    });

    await user.click(
      within(dialog).getByRole("button", { name: "Mark all as read" }),
    );
    await waitFor(() => {
      expect(within(dialog).getByTestId("notifications-empty")).toBeInTheDocument();
    });

    loadMoreGate.release?.(
      jsonResponse({
        unread_count: 1,
        has_more: false,
        items: [notificationItem({ id: 10, title: "Stale page two" })],
      }),
    );

    await waitFor(() => {
      expect(within(dialog).queryByText("Stale page two")).toBeNull();
    });
    expect(within(dialog).getByTestId("notifications-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("notification-unread-badge")).not.toBeInTheDocument();
  });

  it("renders a compact bounded list for many unread rows without card footers", async () => {
    const user = userEvent.setup();
    const items = Array.from({ length: 40 }, (_, index) =>
      notificationItem({
        id: index + 1,
        title: `Compact row ${index + 1}`,
        body: `Body ${index + 1} `.repeat(12),
      }),
    );
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/notifications?") && url.includes("status=unread")) {
        return jsonResponse({
          unread_count: items.length,
          has_more: false,
          items,
        });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      throw new Error(`unexpected ${url}`);
    });

    renderCenter(fetchMock);
    await user.click(
      await screen.findByRole("button", {
        name: "Open notifications (40 unread)",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    const scroll = within(dialog).getByTestId("notifications-scroll");
    const rows = within(scroll).getAllByTestId("notification-row");
    expect(rows).toHaveLength(40);
    expect(scroll.className).toMatch(/overflow-y-auto/);
    expect(scroll.className).toMatch(/max-h-/);
    // Dense rows keep read-state in sr-only text, not a visible footer label.
    expect(within(dialog).queryAllByText("Unread").length).toBeGreaterThan(0);
    for (const label of within(dialog).getAllByText("Unread")) {
      expect(label.className).toMatch(/sr-only/);
    }
    expect(within(dialog).getAllByRole("button", { name: "Mark as read" })).toHaveLength(
      40,
    );
    // Dense rows: no large card footer chrome.
    for (const row of rows.slice(0, 3)) {
      expect(row.className).not.toMatch(/rounded-\[14px\]/);
      expect(row.className).toMatch(/min-h-11/);
      expect(row.querySelector(".border-t")).toBeNull();
    }
  });

  it("defaults absent in-app preferences to enabled and saves a first-click opt-out", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        jsonResponse({ unread_count: 0, has_more: false, items: [] }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          items: [
            {
              id: 2,
              user_id: 7,
              vault_id: 9,
              event: "job_completed",
              channel: "push",
              enabled: true,
            },
          ],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          id: 3,
          user_id: 7,
          vault_id: 9,
          event: "job_completed",
          channel: "in_app",
          enabled: false,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          items: [
            {
              id: 3,
              user_id: 7,
              vault_id: 9,
              event: "job_completed",
              channel: "in_app",
              enabled: false,
            },
          ],
        }),
      );

    renderCenter(fetchMock);
    await user.click(
      await screen.findByRole("button", { name: "Open notifications" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    const inApp = await within(dialog).findByRole("checkbox", {
      name: "Completed jobs: In-app",
    });
    expect(inApp).toBeChecked();
    expect(
      within(dialog).getByRole("checkbox", { name: "Completed jobs: Push" }),
    ).toBeChecked();

    await user.click(inApp);
    expect(inApp).not.toBeChecked();
    await waitFor(() => {
      expect(fetchMock.mock.calls.map((call) => requestUrl(call))).toContain(
        "/api/vault/notification-preferences",
      );
    });
    const preferencePost = fetchMock.mock.calls.find(
      (call) =>
        requestUrl(call) === "/api/vault/notification-preferences" &&
        call[1]?.method === "POST",
    );
    expect(JSON.parse(String(preferencePost?.[1]?.body))).toEqual({
      event: "job_completed",
      channel: "in_app",
      enabled: false,
    });
  });

  it("has explicit loading and error states for inbox requests", async () => {
    const user = userEvent.setup();
    const pendingFetch = vi
      .fn()
      .mockReturnValue(new Promise<Response>(() => undefined));
    const pendingRender = renderCenter(pendingFetch);
    await user.click(
      await screen.findByRole("button", { name: "Open notifications" }),
    );
    expect(await screen.findByTestId("notifications-loading")).toHaveTextContent(
      "Loading notifications",
    );
    pendingRender.unmount();

    const errorFetch = vi.fn().mockRejectedValue(new Error("network down"));
    renderCenter(errorFetch);
    await user.click(
      await screen.findByRole("button", { name: "Open notifications" }),
    );
    expect(await screen.findByTestId("notifications-error")).toBeInTheDocument();
  });

  it("localizes mark-all and show-read controls in Italian", async () => {
    const user = userEvent.setup();
    const itCatalog: Record<string, string> = {
      ...catalog,
      "ui.notifications_open_unread": "Apri notifiche ({count} non lette)",
      "ui.notifications_title": "Notifiche",
      "ui.notifications_mark_all_read": "Segna tutte come lette",
      "ui.notifications_show_read": "Mostra lette",
      "ui.notifications_hide_read": "Nascondi lette",
    };
    const tIt = (key: string, params: Record<string, unknown> = {}) =>
      (itCatalog[key] ?? key).replace(/\{(\w+)\}/g, (match, name: string) =>
        Object.prototype.hasOwnProperty.call(params, name)
          ? String(params[name])
          : match,
      );
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/notifications")) {
        return jsonResponse({
          unread_count: 1,
          has_more: false,
          items: [notificationItem({ id: 1, title: "Ciao" })],
        });
      }
      if (url === "/api/vault/notification-preferences") {
        return jsonResponse({ items: [] });
      }
      throw new Error(url);
    });
    configureApiClient({ fetch: fetchMock, csrfToken: "notification-csrf" });
    render(
      <NotificationCenter
        currentVaultId={9}
        vaultName="Archivio"
        locale="it"
        t={tIt}
        queryClient={createAppQueryClient()}
      />,
    );
    await user.click(
      await screen.findByRole("button", {
        name: "Apri notifiche (1 non lette)",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Notifiche" });
    expect(
      within(dialog).getByRole("button", { name: "Segna tutte come lette" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "Mostra lette" }),
    ).toBeInTheDocument();
  });
});
