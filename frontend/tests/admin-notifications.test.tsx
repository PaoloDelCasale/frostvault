import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
  type ApiFetch,
} from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { NotificationsSection } from "@/pages/admin/NotificationsSection";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);
const messages = JSON.parse(
  readFileSync(path.join(localesDir, "en.json"), "utf8"),
) as Record<string, string>;

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderNotifications(fetchMock: ApiFetch) {
  configureApiClient({ fetch: fetchMock, csrfToken: "csrf" });
  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider initialLocale="en">
        <NotificationsSection />
      </I18nProvider>
    </ApiQueryProvider>,
  );
}

function notificationCatalogResponse() {
  return jsonResponse({
    locale: "en",
    locales: ["en", "it"],
    messages,
  });
}

afterEach(() => {
  cleanup();
  resetApiClientForTests();
});

it("routes webhook configuration through the admin form and shows saving and success states", async () => {
  const user = userEvent.setup();
  const pending = deferred<Response>();
  const bodies: unknown[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) return notificationCatalogResponse();
    if (url === "/api/admin/notification-endpoints/webhook") {
      bodies.push(JSON.parse(String(init?.body)));
      return pending.promise;
    }
    throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
  });

  renderNotifications(fetchMock);
  await screen.findByRole("heading", { name: /notification endpoints/i });
  await user.type(
    screen.getByLabelText(/webhook url/i),
    "https://hooks.example.test/frostvault",
  );
  await user.type(
    screen.getAllByLabelText(/audit reason/i)[0]!,
    "configure webhook",
  );
  await user.click(screen.getByRole("button", { name: /save webhook/i }));

  expect(
    screen.getByRole("button", { name: /saving/i }),
  ).toBeDisabled();
  expect(bodies[0]).toEqual({
    url: "https://hooks.example.test/frostvault",
    enabled: true,
    reason: "configure webhook",
  });

  pending.resolve(
    jsonResponse({ id: 12, kind: "webhook", enabled: true, name: "global-webhook" }),
  );
  expect(await screen.findByRole("status")).toHaveTextContent(
    /webhook endpoint saved/i,
  );
  expect(
    screen.getByRole("button", { name: /save webhook/i }),
  ).toBeEnabled();
});

it("submits SMTP settings, clears the password after success, and never renders response data", async () => {
  const user = userEvent.setup();
  const password = "smtp-write-only-secret";
  const bodies: unknown[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) return notificationCatalogResponse();
    if (url === "/api/admin/notification-endpoints/smtp") {
      bodies.push(JSON.parse(String(init?.body)));
      return jsonResponse({ id: 13, kind: "smtp", enabled: true });
    }
    throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
  });

  renderNotifications(fetchMock);
  await screen.findByRole("heading", { name: /notification endpoints/i });
  await user.type(screen.getByLabelText(/smtp host/i), "smtp.example.test");
  await user.clear(screen.getByLabelText(/smtp port/i));
  await user.type(screen.getByLabelText(/smtp port/i), "2525");
  await user.type(screen.getByLabelText(/smtp username/i), "alerts");
  await user.type(screen.getByLabelText(/smtp password/i), password);
  await user.type(
    screen.getByLabelText(/from address/i),
    "alerts@example.com",
  );
  await user.type(
    screen.getAllByLabelText(/audit reason/i)[1]!,
    "configure email",
  );
  await user.click(screen.getByRole("button", { name: /save smtp settings/i }));

  await waitFor(() => expect(bodies).toHaveLength(1));
  expect(bodies[0]).toMatchObject({
    host: "smtp.example.test",
    port: 2525,
    username: "alerts",
    password,
    from_address: "alerts@example.com",
    use_tls: true,
    enabled: true,
    reason: "configure email",
  });
  expect(screen.getByLabelText(/smtp password/i)).toHaveValue("");
  expect(await screen.findByRole("status")).toHaveTextContent(
    /smtp endpoint saved/i,
  );
});

it("shows a localized error without hiding the rest of the form when saving fails", async () => {
  const user = userEvent.setup();
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) return notificationCatalogResponse();
    if (url === "/api/admin/notification-endpoints/webhook") {
      throw new Error("notification service unavailable");
    }
    throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
  });

  renderNotifications(fetchMock);
  await screen.findByRole("heading", { name: /notification endpoints/i });
  await user.type(
    screen.getByLabelText(/webhook url/i),
    "https://hooks.example.test/frostvault",
  );
  await user.type(
    screen.getAllByLabelText(/audit reason/i)[0]!,
    "retry webhook",
  );
  await user.click(screen.getByRole("button", { name: /save webhook/i }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "notification service unavailable",
  );
  expect(screen.getByLabelText(/webhook url/i)).toHaveValue(
    "https://hooks.example.test/frostvault",
  );
});

it("keeps the notification keys in English and Italian parity", () => {
  const en = JSON.parse(
    readFileSync(path.join(localesDir, "en.json"), "utf8"),
  ) as Record<string, string>;
  const it = JSON.parse(
    readFileSync(path.join(localesDir, "it.json"), "utf8"),
  ) as Record<string, string>;
  const enKeys = Object.keys(en)
    .filter((key) => key.startsWith("admin.notifications_"))
    .sort();
  const itKeys = Object.keys(it)
    .filter((key) => key.startsWith("admin.notifications_"))
    .sort();

  expect(enKeys.length).toBeGreaterThan(0);
  expect(itKeys).toEqual(enKeys);
});
