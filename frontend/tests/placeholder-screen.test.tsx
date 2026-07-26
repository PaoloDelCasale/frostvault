import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import App from "@/App";

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("App shell screen", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    configureApiClient({ fetch: fetchMock });
    window.history.replaceState({}, "", "/");
    fetchMock.mockResolvedValue(
      jsonResponse({
        items: [
          {
            type: "file",
            name: "q1-summary.pdf",
            path: "reports/q1-summary.pdf",
            local_size: 1024,
            state: "both",
            storage_class: "STANDARD",
            cloud_exists: 1,
            local_exists: 1,
          },
        ],
        total: 1,
        page: 1,
        directory: "",
        mode: "browse",
      }),
    );
  });

  afterEach(() => {
    cleanup();
    resetApiClientForTests();
  });

  function renderApp() {
    const client = createAppQueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <ApiQueryProvider client={client}>
        <App />
      </ApiQueryProvider>,
    );
  }

  it("renders the vault shell heading", async () => {
    renderApp();
    expect(
      screen.getByRole("heading", { level: 1, name: "Test Archive" }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("file-browser")).toBeInTheDocument();
    });
  });

  it("renders archive statistics and the file browser without burying the first file", async () => {
    renderApp();
    expect(screen.getByTestId("stats-compact")).toBeInTheDocument();
    expect(screen.getByTestId("archive-file-list")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByText("q1-summary.pdf").length).toBeGreaterThan(0);
    });
    expect(screen.getByTestId("safety-footer")).toBeInTheDocument();
  });
});
