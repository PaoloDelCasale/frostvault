import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import type { AuditEvent } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { AdminPage } from "@/pages/admin/AdminPage";
import { filterAuditEvents } from "@/pages/vault-access/AuditEventsPanel";

import {
  createVaultAccessFetch,
  jsonResponse,
  loadCatalog,
  renderVaultAccess,
} from "./vault-access-harness";

const messages = loadCatalog("en");

function auditEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id: 1,
    created_at: "2026-07-03T12:00:00+00:00",
    event: "vault_file_renamed",
    outcome: "success",
    actor_user_id: 5,
    vault_id: 1,
    job_id: null,
    correlation_id: null,
    visibility: "vault",
    detail: { vault_file_id: "file-abc", new_path: "reports/final.pdf" },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  resetApiClientForTests();
  window.history.replaceState({}, "", "/");
});

describe("audit event filters", () => {
  it("filters only the loaded events by actor, action, and inclusive date range", () => {
    const newest = auditEvent({ id: 3, event: "vault_file_renamed" });
    const otherActor = auditEvent({
      id: 2,
      actor_user_id: 9,
      event: "cloud_deletion.archive_requested",
      created_at: "2026-07-02T12:00:00+00:00",
    });
    const oldest = auditEvent({
      id: 1,
      event: "vault_file_renamed",
      created_at: "2026-07-01T12:00:00+00:00",
    });

    expect(
      filterAuditEvents([newest, otherActor, oldest], {
        actor: "5",
        action: "vault_file_renamed",
        from: "2026-07-01",
        to: "2026-07-03",
      }),
    ).toEqual([newest, oldest]);
  });
});

describe("VaultAccessPage audit panel", () => {
  it("does not mount or request audit events for a non-owner", async () => {
    const { fetchMock } = renderVaultAccess({ isVaultOwner: false });

    await screen.findByRole("heading", { name: "Test Archive", level: 1 });

    expect(screen.queryByRole("heading", { name: "Audit trail", level: 2 })).toBeNull();
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]) === "/api/audit-events"),
    ).toBe(false);
  });

  it("renders loaded-event filters, expandable cards, and member-name fallbacks for an owner", async () => {
    const user = userEvent.setup();
    const fetchMock = createVaultAccessFetch({
      "GET /api/vault/members": jsonResponse({
        items: [
          {
            id: 5,
            username: "ada",
            display_name: "Ada Lovelace",
            role: "owner",
          },
        ],
      }),
      "GET /api/audit-events": jsonResponse({
        events: [
          auditEvent({ id: 3, event: "vault_file_renamed" }),
          auditEvent({
            id: 2,
            actor_user_id: 99,
            event: "cloud_deletion.archive_requested",
            created_at: "2026-07-02T12:00:00+00:00",
          }),
          auditEvent({
            id: 1,
            event: "vault_lifecycle_default_updated",
            created_at: "2026-07-01T12:00:00+00:00",
          }),
        ],
      }),
    });

    renderVaultAccess({ fetchImpl: fetchMock, isVaultOwner: true });

    expect(
      await screen.findByRole("heading", { name: "Audit trail", level: 2 }),
    ).toBeInTheDocument();
    expect((await screen.findAllByText("Ada Lovelace")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("User #99").length).toBeGreaterThan(0);
    expect(
      screen.getByText(/Filters apply only to these loaded events.*complete audit history/i),
    ).toBeInTheDocument();

    const auditList = screen.getByRole("list", { name: "Loaded audit events" });
    expect(within(auditList).getAllByRole("article")).toHaveLength(3);

    await user.selectOptions(screen.getByLabelText("Actor"), "5");
    expect(within(auditList).getAllByRole("article")).toHaveLength(2);

    await user.selectOptions(screen.getByLabelText("Action"), "vault_file_renamed");
    expect(within(auditList).getAllByRole("article")).toHaveLength(1);

    await user.selectOptions(screen.getByLabelText("Action"), "");
    fireEvent.change(screen.getByLabelText("From date"), {
      target: { value: "2026-07-02" },
    });
    expect(within(auditList).getAllByRole("article")).toHaveLength(1);
    expect(
      within(auditList).getByRole("article", { name: "vault_file_renamed" }),
    ).toBeInTheDocument();

    await user.click(screen.getByText("Show event details"));
    expect(screen.getByText("vault_file_id")).toBeInTheDocument();
    expect(screen.getAllByText("file-abc").length).toBeGreaterThan(0);
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]) === "/api/audit-events"),
    ).toHaveLength(1);
  });
});

describe("AdminPage audit route", () => {
  it("uses the dedicated route, resolves authorized names, and falls back to IDs", async () => {
    window.history.replaceState({}, "", "/admin/audit-events");
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) {
        return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
      }
      if (url === "/api/me") {
        return jsonResponse({
          id: 1,
          username: "admin",
          display_name: "Admin",
          is_admin: true,
          active: true,
          session_version: 1,
          csrf_token: "csrf",
          offline_cache_generation: "audit-session-vault-1",
          auth_method: "local",
          locale: "en",
          locales: ["en", "it"],
          vault: null,
        });
      }
      if (url === "/api/admin/audit-events") {
        return jsonResponse({
          events: [
            auditEvent({ id: 10, visibility: "admin", event: "admin_user_created" }),
            auditEvent({
              id: 9,
              actor_user_id: 99,
              vault_id: 88,
              visibility: "admin",
              event: "system_settings.updated",
            }),
          ],
        });
      }
      if (url === "/api/admin/users") {
        return jsonResponse({
          items: [
            {
              id: 5,
              username: "ada",
              display_name: "Ada Lovelace",
              active: true,
              is_admin: true,
              vault_count: 1,
              has_password: true,
              identity_count: 0,
            },
          ],
        });
      }
      if (url === "/api/admin/vaults") {
        return jsonResponse({
          items: [
            {
              id: 1,
              name: "Research",
              slug: "research",
              source_root: "/sources/research",
              s3_prefix: "vaults/research/",
              enabled: true,
              member_count: 1,
            },
          ],
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    configureApiClient({ fetch: fetchMock, csrfToken: "csrf" });
    render(
      <ApiQueryProvider client={createAppQueryClient()}>
        <I18nProvider>
          <AdminPage />
        </I18nProvider>
      </ApiQueryProvider>,
    );

    expect(
      await screen.findByRole("heading", { name: "Audit events", level: 2 }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Ada Lovelace").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Research").length).toBeGreaterThan(0);
    expect(screen.getAllByText("User #99").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Vault #88").length).toBeGreaterThan(0);

    const navigation = screen.getByRole("navigation", {
      name: "Administration sections",
    });
    expect(
      within(navigation).getByRole("link", { name: "Audit events" }),
    ).toHaveAttribute("aria-current", "page");
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]) === "/api/admin/audit-events"),
    ).toHaveLength(1);
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]) === "/api/admin/source-volumes"),
    ).toBe(false);
  });
});
