import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
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
  "ui.notifications_read": "Read",
  "ui.notifications_unread": "Unread",
  "ui.notifications_mark_read": "Mark as read",
  "ui.notifications_marking_read": "Marking as read…",
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

function renderCenter(fetchMock: ReturnType<typeof vi.fn>) {
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

describe("NotificationCenter", () => {
  beforeEach(() => {
    resetApiClientForTests();
  });

  afterEach(() => {
    resetApiClientForTests();
  });

  it("shows the server unread count, renders hostile content as text, marks read, and returns focus on Escape", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        jsonResponse({
          unread_count: 1,
          items: [
            {
              id: 10,
              user_id: 7,
              vault_id: 9,
              job_id: 44,
              event: "job_completed",
              title: "<img src=x onerror=alert(1)>",
              body: "<script>alert(1)</script>",
              title_key: null,
              body_key: null,
              message_params: {},
              in_app_enabled: true,
              dedupe_key: "job:44:job_completed",
              created_at: "2025-01-01T10:00:00Z",
              read: false,
              read_at: null,
            },
          ],
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(
        jsonResponse({
          id: 10,
          user_id: 7,
          vault_id: 9,
          job_id: 44,
          event: "job_completed",
          title: "<img src=x onerror=alert(1)>",
          body: "<script>alert(1)</script>",
          title_key: null,
          body_key: null,
          message_params: {},
          in_app_enabled: true,
          dedupe_key: "job:44:job_completed",
          created_at: "2025-01-01T10:00:00Z",
          read: true,
          read_at: "2025-01-01T10:01:00Z",
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ unread_count: 0, items: [] }));

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

    await user.click(
      within(dialog).getByRole("button", { name: "Mark as read" }),
    );
    await waitFor(() => {
      expect(screen.queryByTestId("notification-unread-badge")).not.toBeInTheDocument();
    });
    expect(fetchMock.mock.calls.map((call) => call[0])).toContain(
      "/api/notifications/read",
    );
    expect(within(dialog).queryByRole("button", { name: "Mark as read" })).toBeNull();

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(bell).toHaveFocus();
  });

  it("loads personal preferences for the selected Vault and optimistically saves channel changes", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ unread_count: 0, items: [] }))
      .mockResolvedValueOnce(
        jsonResponse({
          items: [
            {
              id: 1,
              user_id: 7,
              vault_id: 9,
              event: "job_completed",
              channel: "in_app",
              enabled: false,
            },
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
          enabled: true,
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
              enabled: true,
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
    expect(inApp).not.toBeChecked();
    expect(
      within(dialog).getByRole("checkbox", { name: "Completed jobs: Push" }),
    ).toBeChecked();

    await user.click(inApp);
    expect(inApp).toBeChecked();
    await waitFor(() => {
      expect(fetchMock.mock.calls.map((call) => call[0])).toContain(
        "/api/vault/notification-preferences",
      );
    });
    const preferencePost = fetchMock.mock.calls.find(
      (call) =>
        call[0] === "/api/vault/notification-preferences" &&
        call[1]?.method === "POST",
    );
    expect(JSON.parse(String(preferencePost?.[1]?.body))).toEqual({
      event: "job_completed",
      channel: "in_app",
      enabled: true,
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
});
