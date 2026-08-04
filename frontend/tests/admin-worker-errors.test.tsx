import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import type { AdminWorkerError } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import {
  groupWorkerErrors,
  isExpectedEnvironmentError,
  WorkerErrorsSection,
} from "@/pages/admin/WorkerErrorsSection";

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

function workerError(
  overrides: Partial<AdminWorkerError> = {},
): AdminWorkerError {
  return {
    id: 1,
    created_at: "2026-07-01T00:00:00+00:00",
    component: "background_loop",
    classification: "configuration",
    message: "Rclone configuration not found: /config/rclone/rclone.conf",
    vault_id: null,
    detail: { exception_type: "RuntimeError", event: "scan" },
    ...overrides,
  };
}

afterEach(() => {
  resetApiClientForTests();
});

describe("worker error grouping and environment classification", () => {
  it("groups durable context while ignoring per-occurrence detail", () => {
    const grouped = groupWorkerErrors([
      workerError({
        id: 3,
        created_at: "2026-07-01T00:02:00+00:00",
        detail: { exception_type: "RuntimeError", event: "scan", job_id: 30 },
      }),
      workerError({
        id: 2,
        created_at: "2026-07-01T00:01:00+00:00",
        detail: { exception_type: "OSError", event: "scan", job_id: 20 },
      }),
      workerError({
        id: 4,
        created_at: "2026-07-01T00:03:00+00:00",
        detail: { exception_type: "RuntimeError", event: "audit", job_id: 40 },
      }),
    ]);

    expect(grouped).toHaveLength(2);
    expect(grouped[0]).toMatchObject({
      operation: "audit",
      count: 1,
      first: { id: 4 },
      latest: { id: 4 },
    });
    expect(grouped[1]).toMatchObject({
      operation: "scan",
      count: 2,
      first: { id: 2 },
      latest: { id: 3 },
    });
  });

  it("only treats the two documented local placeholder failures as expected", () => {
    expect(isExpectedEnvironmentError(workerError())).toBe(true);
    expect(
      isExpectedEnvironmentError(
        workerError({ message: "The S3 bucket name is not configured" }),
      ),
    ).toBe(true);
    expect(
      isExpectedEnvironmentError(workerError({ message: "Access denied" })),
    ).toBe(false);
  });
});

describe("WorkerErrorsSection", () => {
  it("shows grouped context, Vault labels/fallbacks, and read-only disposition", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) {
        return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
      }
      if (url === "/api/admin/worker-errors") {
        return jsonResponse({
          items: [
            workerError({ id: 3, created_at: "2026-07-01T00:02:00+00:00" }),
            workerError({ id: 2, created_at: "2026-07-01T00:01:00+00:00" }),
            workerError({
              id: 4,
              component: "notification_delivery",
              classification: "configuration",
              message: "The S3 bucket name is not configured",
              vault_id: 7,
              detail: { exception_type: "RuntimeError", event: "notify" },
            }),
            workerError({
              id: 5,
              component: "upload",
              classification: "permission",
              message: "Access denied while uploading",
              vault_id: 99,
              detail: { exception_type: "PermissionError", event: "upload" },
            }),
          ],
        });
      }
      if (url === "/api/admin/vaults") {
        return jsonResponse({
          items: [
            {
              id: 7,
              name: "Photos",
              slug: "photos",
              source_root: "/sources/photos",
              s3_prefix: "vaults/photos/",
              enabled: true,
              member_count: 1,
            },
          ],
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    configureApiClient({ fetch: fetchMock });
    render(
      <ApiQueryProvider client={createAppQueryClient()}>
        <I18nProvider>
          <WorkerErrorsSection />
        </I18nProvider>
      </ApiQueryProvider>,
    );

    expect(
      await screen.findByRole("heading", { name: "scan", level: 3 }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "notify", level: 3 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "upload", level: 3 })).toBeInTheDocument();
    expect(screen.getByText(/2 occurrence\(s\)/)).toBeInTheDocument();
    expect(screen.getByText("Photos")).toBeInTheDocument();
    expect(screen.getByText("Vault #99")).toBeInTheDocument();
    expect(screen.getAllByText("Expected in this environment")).toHaveLength(2);
    expect(screen.getByText("Actionable")).toBeInTheDocument();
    const articles = screen.getAllByRole("article");
    expect(articles).toHaveLength(3);
    for (const article of articles) {
      const headingId = article.getAttribute("aria-labelledby");
      expect(headingId).toBeTruthy();
      expect(headingId).not.toMatch(/\s/);
      expect(document.getElementById(headingId ?? "")).toBe(
        article.querySelector("h3"),
      );
    }
    expect(screen.getByRole("article", { name: "scan" })).toHaveAccessibleName(
      "scan",
    );
    expect(screen.getByText(/2026-07-01T00:01:00/)).toBeInTheDocument();
    expect(screen.getAllByText(/2026-07-01T00:02:00/).length).toBeGreaterThan(0);
    expect(
      screen.getByText(
        "This is a read-only view. Clearing or acknowledging worker errors is not supported by the API.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /clear|acknowledge/i })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(fetchMock.mock.calls.map((call) => String(call[0]))).toContain(
        "/api/admin/worker-errors",
      );
    });
  });
});
