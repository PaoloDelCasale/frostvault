import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { FilesystemFinding, FilesystemHealth } from "@/api/types";
import { FilesystemHealthBanner } from "@/pages/archive/FilesystemHealthBanner";
import {
  DETAILS_PAGE_SIZE,
  INLINE_FINDING_SAMPLE,
  groupFilesystemFindings,
} from "@/pages/archive/filesystemHealthFindings";

const messages: Record<string, string> = {
  "ui.filesystem_healthy": "Vault filesystem healthy",
  "ui.filesystem_needs_attention": "Vault filesystem needs attention",
  "ui.filesystem_attention_detail":
    "Symbolic links and permission errors are reported; ownership and modes are never changed automatically.",
  "ui.source_volume_degraded": "Local storage for this Source Volume is unavailable",
  "ui.source_volume_degraded_detail":
    "Catalog and cloud operations remain available. Remount /sources/<alias> as a direct rw volume, then run a full local scan before local operations resume.",
  "ui.filesystem_findings_summary_one": "{count} finding in {groups} group",
  "ui.filesystem_findings_summary_other": "{count} findings in {groups} groups",
  "ui.filesystem_findings_truncated":
    "Showing {shown} of {total} findings. Open details to inspect every path.",
  "ui.filesystem_finding_group": "{message} ({code}) — {count} path(s) under {scope}",
  "ui.filesystem_finding_group_root": "{message} ({code}) — {count} path(s)",
  "ui.filesystem_show_all_findings": "Show all {count} findings",
  "ui.filesystem_findings_details_title": "Filesystem findings",
  "ui.filesystem_findings_details_description":
    "Every diagnostic returned for this Vault. Remediation is listed with each group.",
  "ui.filesystem_findings_page": "Page {page} of {pages}",
  "ui.filesystem_findings_prev_page": "Previous findings page",
  "ui.filesystem_findings_next_page": "Next findings page",
  "ui.close": "Close",
};

function t(key: string, params?: Record<string, string | number>): string {
  const raw = messages[key] ?? key;
  if (!params) return raw;
  return raw.replace(/\{(\w+)\}/g, (match, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name])
      : match,
  );
}

const healthy: FilesystemHealth = {
  ok: true,
  uid: 1000,
  gid: 1000,
  root: "/sources/test",
  checks: [
    {
      code: "fs.identity",
      status: "pass",
      message: "Effective identity is uid=1000 gid=1000",
    },
  ],
  findings: [],
};

const unhealthy: FilesystemHealth = {
  ok: false,
  uid: 1000,
  gid: 1000,
  root: "/sources/test",
  checks: [
    {
      code: "fs.entries",
      status: "fail",
      message: "2 filesystem problem(s) under the vault root",
      remediation:
        "Fix host permissions for the reported paths or remove symbolic links; the archive never changes ownership or modes",
    },
  ],
  findings: [
    {
      path: "alias.txt",
      code: "fs.symlink",
      message: "Symbolic link rejected: alias.txt",
      remediation: "Remove the symbolic link or replace it with a regular file",
    },
    {
      path: "secret.bin",
      code: "fs.unreadable_file",
      message: "File is unreadable: secret.bin",
      remediation: "Grant read permission on secret.bin for the archive user",
    },
  ],
};

function makeFinding(
  index: number,
  overrides: Partial<FilesystemFinding> = {},
): FilesystemFinding {
  const scope = index % 3 === 0 ? "clips" : index % 3 === 1 ? "exports" : "recordings";
  return {
    path: `${scope}/cam${index % 7}/file-${index}`,
    code: "fs.unwritable_directory",
    message: "Directory is not writable",
    remediation:
      "Grant write permission on the directory for the archive runtime identity",
    ...overrides,
  };
}

describe("groupFilesystemFindings", () => {
  it("groups repeated findings by code and top-level path scope", () => {
    const findings = [
      makeFinding(0, { path: "clips/a" }),
      makeFinding(1, { path: "clips/b" }),
      makeFinding(2, { path: "exports/a" }),
      makeFinding(3, {
        path: "root-file",
        code: "fs.symlink",
        message: "Symbolic link rejected",
        remediation: "Remove the symbolic link",
      }),
    ];

    const groups = groupFilesystemFindings(findings);
    expect(groups).toHaveLength(3);

    const clips = groups.find((group) => group.scope === "clips");
    expect(clips?.code).toBe("fs.unwritable_directory");
    expect(clips?.count).toBe(2);
    expect(clips?.remediation).toMatch(/Grant write permission/);

    const exportsGroup = groups.find((group) => group.scope === "exports");
    expect(exportsGroup?.count).toBe(1);

    const rootFileGroup = groups.find((group) => group.scope === "root-file");
    expect(rootFileGroup?.code).toBe("fs.symlink");
    expect(rootFileGroup?.count).toBe(1);
  });
});

describe("FilesystemHealthBanner", () => {
  it("shows no alarm banner when filesystem.ok is true", () => {
    render(<FilesystemHealthBanner filesystem={healthy} t={t} />);

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/needs attention/i)).not.toBeInTheDocument();
  });

  it("shows a compact warn banner for small finding sets with every path and group remediation", () => {
    render(<FilesystemHealthBanner filesystem={unhealthy} t={t} />);

    const alarm = screen.getByRole("alert");
    expect(alarm).toHaveClass("warn");
    expect(alarm).toHaveTextContent(/needs attention/i);
    expect(alarm).toHaveTextContent("2 findings in 2 groups");

    expect(alarm).toHaveTextContent("alias.txt");
    expect(alarm).toHaveTextContent("Symbolic link rejected: alias.txt");
    expect(alarm).toHaveTextContent(
      "Remove the symbolic link or replace it with a regular file",
    );

    expect(alarm).toHaveTextContent("secret.bin");
    expect(alarm).toHaveTextContent("File is unreadable: secret.bin");
    expect(alarm).toHaveTextContent(
      "Grant read permission on secret.bin for the archive user",
    );

    // Small sets stay fully visible without forcing the details dialog open.
    expect(
      screen.queryByRole("dialog", { name: /Filesystem findings/i }),
    ).not.toBeInTheDocument();
  });

  it("still displays a scan finding that is absent from the live preflight checks", () => {
    // Backend merges scan-time findings into filesystem.findings (see app/main.py
    // stats()). The UI must render the full array and must not drop entries that
    // are not also represented among live preflight checks.
    const merged: FilesystemHealth = {
      ok: false,
      uid: 1000,
      gid: 1000,
      root: "/sources/test",
      checks: [
        {
          code: "fs.entries",
          status: "fail",
          message: "1 filesystem problem(s) under the vault root",
          remediation:
            "Fix host permissions for the reported paths or remove symbolic links",
        },
      ],
      findings: [
        {
          path: "link.txt",
          code: "fs.symlink",
          message: "Symbolic link rejected: link.txt",
        },
        {
          // Produced by scan_tree, not by live check_vault_filesystem.
          path: "secret.bin",
          code: "fs.unreadable_file",
          message: "Permission denied while reading secret.bin",
        },
      ],
    };

    render(<FilesystemHealthBanner filesystem={merged} t={t} />);

    const alarm = screen.getByRole("alert");
    expect(alarm).toHaveTextContent("link.txt");
    expect(alarm).toHaveTextContent("secret.bin");
    expect(alarm).toHaveTextContent(
      "Permission denied while reading secret.bin",
    );
  });

  it("groups repeated unwritable findings and shows remediation once per group", () => {
    const repeated: FilesystemHealth = {
      ok: false,
      uid: 99,
      gid: 100,
      root: "/sources/cameras",
      checks: [
        {
          code: "fs.entries",
          status: "fail",
          message: "6 filesystem problem(s) under the vault root",
          remediation: "Fix host permissions for the reported paths",
        },
      ],
      findings: [
        makeFinding(0, { path: "clips" }),
        makeFinding(1, { path: "clips/cam1" }),
        makeFinding(2, { path: "clips/cam1/2024" }),
        makeFinding(3, { path: "exports" }),
        makeFinding(4, { path: "exports/share" }),
        makeFinding(5, { path: "recordings/cam2" }),
      ],
    };

    render(<FilesystemHealthBanner filesystem={repeated} t={t} />);

    const alarm = screen.getByRole("alert");
    expect(alarm).toHaveTextContent("6 findings in 3 groups");
    expect(alarm).toHaveTextContent(/under clips/i);
    expect(alarm).toHaveTextContent(/under exports/i);
    expect(alarm).toHaveTextContent(/under recordings/i);

    const remediationMatches = within(alarm).getAllByText(
      /Grant write permission on the directory for the archive runtime identity/,
    );
    // One remediation line per group, not one per path.
    expect(remediationMatches).toHaveLength(3);

    // Representative samples stay inline; not every descendant needs a top-level row.
    expect(alarm).toHaveTextContent("clips");
    expect(alarm.querySelectorAll('[data-testid="filesystem-finding-row"]').length).toBeLessThanOrEqual(
      INLINE_FINDING_SAMPLE,
    );
  });

  it("keeps the default banner bounded for thousands of findings and exposes full details on demand", async () => {
    const user = userEvent.setup();
    const total = 5000;
    const findings = Array.from({ length: total }, (_, index) => makeFinding(index));
    const large: FilesystemHealth = {
      ok: false,
      uid: 99,
      gid: 100,
      root: "/sources/cameras",
      checks: [
        {
          code: "fs.entries",
          status: "fail",
          message: `${total} filesystem problem(s) under the vault root`,
          remediation: "Fix host permissions for the reported paths",
        },
      ],
      findings,
    };

    const { container } = render(
      <FilesystemHealthBanner filesystem={large} t={t} />,
    );

    const alarm = screen.getByRole("alert");
    expect(alarm).toHaveTextContent("5000 findings in 3 groups");
    expect(alarm).toHaveTextContent(
      `Showing ${INLINE_FINDING_SAMPLE} of ${total} findings`,
    );

    const inlineRows = container.querySelectorAll(
      '[data-testid="filesystem-finding-row"]',
    );
    expect(inlineRows.length).toBeLessThanOrEqual(INLINE_FINDING_SAMPLE);
    expect(inlineRows.length).toBeLessThan(total);

    // Page-level DOM stays bounded: no 5k list items while collapsed.
    expect(
      container.querySelectorAll('[data-testid="filesystem-finding-detail-row"]').length,
    ).toBe(0);

    await user.click(
      screen.getByRole("button", { name: `Show all ${total} findings` }),
    );

    const dialog = await screen.findByRole("dialog", {
      name: /Filesystem findings/i,
    });
    expect(dialog).toBeInTheDocument();

    const detailRows = within(dialog).getAllByTestId(
      "filesystem-finding-detail-row",
    );
    expect(detailRows.length).toBeLessThanOrEqual(DETAILS_PAGE_SIZE);
    expect(detailRows.length).toBeGreaterThan(0);

    // First page still exposes path/code/message/remediation.
    expect(detailRows[0]).toHaveTextContent("fs.unwritable_directory");
    expect(detailRows[0]).toHaveTextContent("Directory is not writable");
    expect(detailRows[0]).toHaveTextContent(/Grant write permission/);

    // Paginate to prove every finding remains reachable without dumping all rows.
    const pages = Math.ceil(total / DETAILS_PAGE_SIZE);
    expect(within(dialog).getByText(`Page 1 of ${pages}`)).toBeInTheDocument();

    await user.click(
      within(dialog).getByRole("button", { name: /Next findings page/i }),
    );
    expect(within(dialog).getByText(`Page 2 of ${pages}`)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /Close/i }));
    expect(
      screen.queryByRole("dialog", { name: /Filesystem findings/i }),
    ).not.toBeInTheDocument();

    // Collapsing restores the compact page-level banner.
    expect(
      container.querySelectorAll('[data-testid="filesystem-finding-detail-row"]').length,
    ).toBe(0);
    expect(screen.getByRole("alert")).toHaveTextContent("5000 findings in 3 groups");
  });

  it("continues to display source-volume degradation diagnostics without findings", () => {
    const degraded: FilesystemHealth = {
      ok: false,
      uid: null,
      gid: null,
      root: "/sources/photos",
      checks: [
        {
          code: "source_volume.unmounted",
          status: "fail",
          message: "Source Volume mount is missing",
          remediation: "Remount /sources/photos as a direct rw volume",
        },
      ],
      findings: [],
      source_volume: {
        alias: "photos",
        health: "degraded",
        local_operations_allowed: false,
        cloud_catalog_allowed: true,
      },
    };

    render(<FilesystemHealthBanner filesystem={degraded} t={t} />);

    const alarm = screen.getByRole("alert");
    expect(alarm).toHaveTextContent(/Source Volume is unavailable/i);
    expect(alarm).toHaveTextContent(/Remount \/sources\/photos/i);
    expect(
      screen.queryByRole("button", { name: /Show all/i }),
    ).not.toBeInTheDocument();
  });

  it("covers English and Italian plural summary labels", () => {
    const it = (key: string, params?: Record<string, string | number>) => {
      const itMessages: Record<string, string> = {
        "ui.filesystem_needs_attention": "Il filesystem del vault richiede attenzione",
        "ui.filesystem_attention_detail": "Dettaglio",
        "ui.filesystem_findings_summary_one": "{count} problema in {groups} gruppo",
        "ui.filesystem_findings_summary_other":
          "{count} problemi in {groups} gruppi",
        "ui.filesystem_findings_truncated":
          "Mostro {shown} di {total} problemi. Apri i dettagli per ogni percorso.",
        "ui.filesystem_finding_group":
          "{message} ({code}) — {count} percorso/i sotto {scope}",
        "ui.filesystem_finding_group_root":
          "{message} ({code}) — {count} percorso/i",
        "ui.filesystem_show_all_findings": "Mostra tutti i {count} problemi",
        "ui.filesystem_findings_details_title": "Problemi del filesystem",
        "ui.filesystem_findings_details_description": "Ogni diagnostica del Vault.",
        "ui.filesystem_findings_page": "Pagina {page} di {pages}",
        "ui.filesystem_findings_prev_page": "Pagina problemi precedente",
        "ui.filesystem_findings_next_page": "Pagina problemi successiva",
        "ui.close": "Chiudi",
      };
      const raw = itMessages[key] ?? key;
      if (!params) return raw;
      return raw.replace(/\{(\w+)\}/g, (match, name: string) =>
        Object.prototype.hasOwnProperty.call(params, name)
          ? String(params[name])
          : match,
      );
    };

    const single: FilesystemHealth = {
      ok: false,
      uid: 1,
      gid: 1,
      root: "/sources/test",
      checks: [],
      findings: [
        {
          path: "only.txt",
          code: "fs.symlink",
          message: "Symbolic link rejected",
        },
      ],
    };

    const { rerender } = render(
      <FilesystemHealthBanner filesystem={single} t={it} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "1 problema in 1 gruppo",
    );

    rerender(<FilesystemHealthBanner filesystem={unhealthy} t={it} />);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "2 problemi in 2 gruppi",
    );
  });
});
