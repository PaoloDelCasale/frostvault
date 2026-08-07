import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { FilesystemFinding, FilesystemHealth } from "@/api/types";
import { FilesystemHealthBanner } from "@/pages/archive/FilesystemHealthBanner";
import {
  DETAILS_GROUP_PAGE_SIZE,
  DETAILS_PAGE_SIZE,
  INLINE_FINDING_SAMPLE,
  INLINE_GROUP_LIMIT,
  groupFilesystemFindings,
} from "@/pages/archive/filesystemHealthFindings";
import { translate } from "@/i18n/translate";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);

const enCatalog = JSON.parse(
  readFileSync(path.join(localesDir, "en.json"), "utf8"),
) as Record<string, string>;
const itCatalog = JSON.parse(
  readFileSync(path.join(localesDir, "it.json"), "utf8"),
) as Record<string, string>;

function tEn(key: string, params?: Record<string, string | number>): string {
  return translate(enCatalog, key, params);
}

function tIt(key: string, params?: Record<string, string | number>): string {
  return translate(itCatalog, key, params);
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

function makeUniqueScopeFinding(index: number): FilesystemFinding {
  return makeFinding(index, {
    path: `scope-${index}/nested/file-${index}`,
  });
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
    render(<FilesystemHealthBanner filesystem={healthy} t={tEn} />);

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/needs attention/i)).not.toBeInTheDocument();
  });

  it("shows a compact warn banner for small finding sets with every path and group remediation", () => {
    render(<FilesystemHealthBanner filesystem={unhealthy} t={tEn} />);

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

    // Root-level single paths use singular "path", never path(s).
    expect(alarm).toHaveTextContent("— 1 path");
    expect(alarm).not.toHaveTextContent("path(s)");
    expect(alarm).not.toHaveTextContent("group(s)");

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

    render(<FilesystemHealthBanner filesystem={merged} t={tEn} />);

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

    render(<FilesystemHealthBanner filesystem={repeated} t={tEn} />);

    const alarm = screen.getByRole("alert");
    expect(alarm).toHaveTextContent("6 findings in 3 groups");
    expect(alarm).toHaveTextContent(/3 paths under clips/i);
    expect(alarm).toHaveTextContent(/2 paths under exports/i);
    expect(alarm).toHaveTextContent(/1 path under recordings/i);
    expect(alarm).not.toHaveTextContent("path(s)");
    expect(alarm).not.toHaveTextContent("group(s)");

    const remediationMatches = within(alarm).getAllByText(
      /Grant write permission on the directory for the archive runtime identity/,
    );
    // One remediation line per group, not one per path.
    expect(remediationMatches).toHaveLength(3);

    // Representative samples stay inline; not every descendant needs a top-level row.
    expect(alarm).toHaveTextContent("clips");
    expect(
      alarm.querySelectorAll('[data-testid="filesystem-finding-row"]').length,
    ).toBeLessThanOrEqual(INLINE_FINDING_SAMPLE);
  });

  it("pluralizes finding and group counts independently in English and Italian", () => {
    const twoFindingsOneGroup: FilesystemHealth = {
      ok: false,
      uid: 1,
      gid: 1,
      root: "/sources/test",
      checks: [],
      findings: [
        makeFinding(0, { path: "clips/a" }),
        makeFinding(1, { path: "clips/b" }),
      ],
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
      <FilesystemHealthBanner filesystem={twoFindingsOneGroup} t={tEn} />,
    );
    expect(screen.getByTestId("filesystem-findings-summary")).toHaveTextContent(
      "2 findings in 1 group",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("2 paths under clips");
    expect(screen.getByRole("alert")).not.toHaveTextContent("path(s)");
    expect(screen.getByRole("alert")).not.toHaveTextContent("group(s)");

    rerender(<FilesystemHealthBanner filesystem={single} t={tEn} />);
    expect(screen.getByTestId("filesystem-findings-summary")).toHaveTextContent(
      "1 finding in 1 group",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("— 1 path");

    rerender(<FilesystemHealthBanner filesystem={unhealthy} t={tEn} />);
    expect(screen.getByTestId("filesystem-findings-summary")).toHaveTextContent(
      "2 findings in 2 groups",
    );

    rerender(
      <FilesystemHealthBanner filesystem={twoFindingsOneGroup} t={tIt} />,
    );
    expect(screen.getByTestId("filesystem-findings-summary")).toHaveTextContent(
      "2 problemi in 1 gruppo",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "2 percorsi sotto clips",
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent("percorso/i");
    expect(screen.getByRole("alert")).not.toHaveTextContent("gruppo/i");

    rerender(<FilesystemHealthBanner filesystem={single} t={tIt} />);
    expect(screen.getByTestId("filesystem-findings-summary")).toHaveTextContent(
      "1 problema in 1 gruppo",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("— 1 percorso");

    rerender(<FilesystemHealthBanner filesystem={unhealthy} t={tIt} />);
    expect(screen.getByTestId("filesystem-findings-summary")).toHaveTextContent(
      "2 problemi in 2 gruppi",
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
      <FilesystemHealthBanner filesystem={large} t={tEn} />,
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
      container.querySelectorAll('[data-testid="filesystem-finding-detail-row"]')
        .length,
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

    // Paginate findings to prove every finding remains reachable without dumping all rows.
    const findingPages = Math.ceil(total / DETAILS_PAGE_SIZE);
    expect(
      within(dialog).getByText(`Findings page 1 of ${findingPages}`),
    ).toBeInTheDocument();

    await user.click(
      within(dialog).getByRole("button", { name: /Next findings page/i }),
    );
    expect(
      within(dialog).getByText(`Findings page 2 of ${findingPages}`),
    ).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /Close/i }));
    expect(
      screen.queryByRole("dialog", { name: /Filesystem findings/i }),
    ).not.toBeInTheDocument();

    // Collapsing restores the compact page-level banner.
    expect(
      container.querySelectorAll('[data-testid="filesystem-finding-detail-row"]')
        .length,
    ).toBe(0);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "5000 findings in 3 groups",
    );
  });

  it("keeps detail group rendering bounded for thousands of distinct scopes", async () => {
    const user = userEvent.setup();
    const totalGroups = 3000;
    const findings = Array.from({ length: totalGroups }, (_, index) =>
      makeUniqueScopeFinding(index),
    );
    const manyScopes: FilesystemHealth = {
      ok: false,
      uid: 99,
      gid: 100,
      root: "/sources/cameras",
      checks: [
        {
          code: "fs.entries",
          status: "fail",
          message: `${totalGroups} filesystem problem(s) under the vault root`,
          remediation: "Fix host permissions for the reported paths",
        },
      ],
      findings,
    };

    const { container } = render(
      <FilesystemHealthBanner filesystem={manyScopes} t={tEn} />,
    );

    const alarm = screen.getByRole("alert");
    expect(alarm).toHaveTextContent(
      `${totalGroups} findings in ${totalGroups} groups`,
    );
    expect(
      alarm.querySelectorAll('[data-testid="filesystem-finding-group"]').length,
    ).toBeLessThanOrEqual(INLINE_GROUP_LIMIT);
    expect(alarm).toHaveTextContent(
      `And ${totalGroups - INLINE_GROUP_LIMIT} more groups`,
    );
    expect(alarm).not.toHaveTextContent("group(s)");

    // Collapsed page never mounts thousands of group detail cards.
    expect(
      container.querySelectorAll(
        '[data-testid="filesystem-finding-group-detail"]',
      ).length,
    ).toBe(0);

    await user.click(
      screen.getByRole("button", { name: `Show all ${totalGroups} findings` }),
    );

    const dialog = await screen.findByRole("dialog", {
      name: /Filesystem findings/i,
    });

    const detailGroups = within(dialog).getAllByTestId(
      "filesystem-finding-group-detail",
    );
    expect(detailGroups.length).toBeLessThanOrEqual(DETAILS_GROUP_PAGE_SIZE);
    expect(detailGroups.length).toBeGreaterThan(0);
    expect(detailGroups.length).toBeLessThan(totalGroups);

    const groupPages = Math.ceil(totalGroups / DETAILS_GROUP_PAGE_SIZE);
    expect(
      within(dialog).getByText(`Groups page 1 of ${groupPages}`),
    ).toBeInTheDocument();

    await user.click(
      within(dialog).getByRole("button", { name: /Next groups page/i }),
    );
    expect(
      within(dialog).getByText(`Groups page 2 of ${groupPages}`),
    ).toBeInTheDocument();

    const pageTwoGroups = within(dialog).getAllByTestId(
      "filesystem-finding-group-detail",
    );
    expect(pageTwoGroups.length).toBeLessThanOrEqual(DETAILS_GROUP_PAGE_SIZE);
    // Paths from page 2 remain inspectable with remediation.
    expect(pageTwoGroups[0]).toHaveTextContent("fs.unwritable_directory");
    expect(pageTwoGroups[0]).toHaveTextContent(/Grant write permission/);

    // Findings list stays independently paginated and bounded.
    expect(
      within(dialog).getAllByTestId("filesystem-finding-detail-row").length,
    ).toBeLessThanOrEqual(DETAILS_PAGE_SIZE);
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

    render(<FilesystemHealthBanner filesystem={degraded} t={tEn} />);

    const alarm = screen.getByRole("alert");
    expect(alarm).toHaveTextContent(/Source Volume is unavailable/i);
    expect(alarm).toHaveTextContent(/Remount \/sources\/photos/i);
    expect(
      screen.queryByRole("button", { name: /Show all/i }),
    ).not.toBeInTheDocument();
  });

  it("uses real locale catalogs without slash-style or (s) plural stubs", () => {
    const banned = ["path(s)", "group(s)", "gruppo/i", "percorso/i", "finding(s)"];
    const keys = Object.keys(enCatalog).filter(
      (key) =>
        key.startsWith("ui.filesystem_finding") ||
        key.startsWith("ui.filesystem_findings") ||
        key.startsWith("ui.filesystem_groups"),
    );
    for (const key of keys) {
      for (const stub of banned) {
        expect(enCatalog[key], `${key} en`).not.toContain(stub);
        expect(itCatalog[key], `${key} it`).not.toContain(stub);
      }
    }

    // Independent plural matrix via real catalogs.
    expect(
      translate(enCatalog, "ui.filesystem_findings_summary", {
        findings: translate(enCatalog, "ui.filesystem_findings_count_other", {
          count: 2,
        }),
        groups: translate(enCatalog, "ui.filesystem_groups_count_one", {
          count: 1,
        }),
      }),
    ).toBe("2 findings in 1 group");

    expect(
      translate(itCatalog, "ui.filesystem_findings_summary", {
        findings: translate(itCatalog, "ui.filesystem_findings_count_other", {
          count: 2,
        }),
        groups: translate(itCatalog, "ui.filesystem_groups_count_one", {
          count: 1,
        }),
      }),
    ).toBe("2 problemi in 1 gruppo");

    expect(
      translate(enCatalog, "ui.filesystem_finding_group_one", {
        message: "Directory is not writable",
        code: "fs.unwritable_directory",
        count: 1,
        scope: "recordings",
      }),
    ).toBe(
      "Directory is not writable (fs.unwritable_directory) — 1 path under recordings",
    );
    expect(
      translate(enCatalog, "ui.filesystem_finding_group_other", {
        message: "Directory is not writable",
        code: "fs.unwritable_directory",
        count: 3,
        scope: "clips",
      }),
    ).toBe(
      "Directory is not writable (fs.unwritable_directory) — 3 paths under clips",
    );
  });
});
