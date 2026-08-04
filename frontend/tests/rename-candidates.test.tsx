import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
} from "@/api";
import { RenameCandidatesPanel } from "@/pages/archive/RenameCandidatesPanel";

const messages: Record<string, string> = {
  "ui.rename_candidates_title": "Rename candidates",
  "ui.rename_candidates_intro": "Review evidence.",
  "ui.file_rename_candidates": "File rename candidates",
  "ui.folder_rename_candidates": "Folder rename candidates",
  "ui.rename_candidate_statement":
    "Local Copy appears to be this Vault File under a new name",
  "ui.previous_path": "Previous path",
  "ui.new_path": "New path",
  "ui.fingerprint_evidence": "Fingerprint evidence",
  "ui.size_evidence": "Size evidence",
  "ui.size_evidence_unavailable": "Not provided by the rename-candidate API",
  "ui.rename_confirm_consequence":
    "Confirming preserves the Vault File identity and appends the new path to Path History.",
  "ui.rename_reject_consequence":
    "If you do not confirm, the Local Copy at the new path remains a new Vault File.",
  "ui.confirm_file_rename": "Confirm file rename",
  "ui.confirm_folder_rename": "Confirm folder rename",
  "ui.confirming_rename": "Confirming…",
  "ui.dismiss_rename_candidate": "Dismiss for now",
  "ui.rename_dismissed_for_now":
    "Dismissed for now. The new path remains a new Vault File unless you confirm.",
  "ui.rename_dismissal_not_persisted":
    "Dismissal is not saved by the API. Candidates can appear again after refresh or restart.",
  "ui.refresh_rename_candidates": "Refresh candidates",
  "ui.rename_candidates_error": "Unable to load rename candidates for this Vault.",
  "ui.rename_confirmed": "Rename confirmed. Vault state has been refreshed.",
  "ui.rename_confirmation_uncertain": "The outcome is uncertain.",
  "ui.rename_read_only": "This Vault is read-only; confirmation is unavailable.",
  "ui.folder_rename_evidence": "{count} matching file pairs suggest that this folder moved.",
  "ui.folder_rename_scope": "Confirming applies the folder endpoint.",
};

function t(key: string, params?: Record<string, string | number>): string {
  const template = messages[key] ?? key;
  return Object.entries(params ?? {}).reduce(
    (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
    template,
  );
}

const candidates = {
  items: [
    {
      missing_vault_file_id: "00000000-0000-0000-0000-000000000001",
      missing_path: "reports-old/a.txt",
      new_vault_file_id: "00000000-0000-0000-0000-000000000011",
      new_path: "reports-new/a.txt",
      digest: "aaaa1111",
      decision: "ambiguous",
      size: 1024,
    },
    {
      missing_vault_file_id: "00000000-0000-0000-0000-000000000002",
      missing_path: "reports-old/b.txt",
      new_vault_file_id: "00000000-0000-0000-0000-000000000012",
      new_path: "reports-new/b.txt",
      digest: "bbbb2222",
      decision: "ambiguous",
    },
  ],
};

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("RenameCandidatesPanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    document.cookie = "frostvault_csrf=rename-panel-csrf";
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/rename-candidates") return jsonResponse(candidates);
      if (path === "/api/confirm-rename") return jsonResponse({ ok: true }, 202);
      if (path === "/api/confirm-folder-rename") return jsonResponse({ ok: true }, 202);
      throw new Error(`Unexpected request: ${path}`);
    });
    configureApiClient({ fetch: fetchMock });
  });

  function renderPanel(canOperate = true) {
    return render(
      <ApiQueryProvider client={createAppQueryClient()}>
        <RenameCandidatesPanel vaultId={7} canOperate={canOperate} t={t} />
      </ApiQueryProvider>,
    );
  }

  it("shows available path, fingerprint, and size evidence and confirms a file", async () => {
    const user = userEvent.setup();
    renderPanel();

    const panel = await screen.findByTestId("rename-candidates");
    expect(
      within(panel).getAllByText(
        "Local Copy appears to be this Vault File under a new name",
      ),
    ).toHaveLength(2);
    expect(within(panel).getByText("reports-old/a.txt")).toBeInTheDocument();
    expect(within(panel).getByText("reports-new/a.txt")).toBeInTheDocument();
    expect(within(panel).getByText("aaaa1111")).toBeInTheDocument();
    expect(within(panel).getByText("1.0 KB")).toBeInTheDocument();
    expect(
      within(panel).getByText("Not provided by the rename-candidate API"),
    ).toBeInTheDocument();
    expect(within(panel).getAllByText(/new Vault File/).length).toBeGreaterThan(0);

    await user.click(within(panel).getAllByRole("button", { name: "Confirm file rename" })[0]!);

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        (call) => call[0] === "/api/confirm-rename",
      );
      expect(post?.[1]).toMatchObject({
        method: "POST",
        body: JSON.stringify({
          vault_file_id: "00000000-0000-0000-0000-000000000001",
          new_path: "reports-new/a.txt",
        }),
      });
      expect(new Headers(post?.[1]?.headers).get("X-CSRF-Token")).toBe(
        "rename-panel-csrf",
      );
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Rename confirmed");
  });

  it("derives a folder candidate from two same-name pairs and uses the folder endpoint", async () => {
    const user = userEvent.setup();
    renderPanel();

    const folderList = await screen.findByRole("list", {
      name: "Folder rename candidates",
    });
    expect(within(folderList).getByText("reports-old")).toBeInTheDocument();
    expect(within(folderList).getByText("reports-new")).toBeInTheDocument();
    expect(within(folderList).getByText(/2 matching file pairs/)).toBeInTheDocument();

    await user.click(
      within(folderList).getByRole("button", { name: "Confirm folder rename" }),
    );

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        (call) => call[0] === "/api/confirm-folder-rename",
      );
      expect(post?.[1]).toMatchObject({
        method: "POST",
        body: JSON.stringify({
          old_prefix: "reports-old",
          new_prefix: "reports-new",
        }),
      });
    });
  });

  it("refetches after a partial-failure response because identity may already be committed", async () => {
    const user = userEvent.setup();
    let candidateReads = 0;
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/rename-candidates") {
        candidateReads += 1;
        return jsonResponse(candidates);
      }
      if (path === "/api/confirm-rename") {
        return jsonResponse({ detail: "queue failed" }, 500);
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    renderPanel();

    const panel = await screen.findByTestId("rename-candidates");
    await user.click(
      within(panel).getAllByRole("button", { name: "Confirm file rename" })[0]!,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The outcome is uncertain.",
    );
    await waitFor(() => expect(candidateReads).toBeGreaterThanOrEqual(2));
    expect(
      within(panel).getAllByText(
        "Local Copy appears to be this Vault File under a new name",
      ),
    ).toHaveLength(2);
  });

  it("dismisses only in memory, states the consequence, and preserves read-only access", async () => {
    const user = userEvent.setup();
    renderPanel(false);

    const panel = await screen.findByTestId("rename-candidates");
    expect(
      within(panel).getAllByRole("button", { name: "Confirm file rename" })[0],
    ).toBeDisabled();
    expect(within(panel).getAllByText(/read-only/)).toHaveLength(2);

    await user.click(within(panel).getAllByRole("button", { name: "Dismiss for now" })[0]!);

    expect(await screen.findByRole("status")).toHaveTextContent(
      "new path remains a new Vault File",
    );
    expect(within(panel).getByText(/Dismissal is not saved by the API/)).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some((call) => call[1]?.method === "POST"),
    ).toBe(false);
  });
});
