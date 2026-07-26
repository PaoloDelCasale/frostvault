import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { FilesystemHealth } from "@/api/types";
import { FilesystemHealthBanner } from "./FilesystemHealthBanner";

const messages: Record<string, string> = {
  "ui.filesystem_healthy": "Vault filesystem healthy",
  "ui.filesystem_needs_attention": "Vault filesystem needs attention",
  "ui.filesystem_attention_detail":
    "Symbolic links and permission errors are reported; ownership and modes are never changed automatically.",
};

function t(key: string): string {
  return messages[key] ?? key;
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

describe("FilesystemHealthBanner", () => {
  it("shows no alarm banner when filesystem.ok is true", () => {
    render(<FilesystemHealthBanner filesystem={healthy} t={t} />);

    expect(
      screen.queryByRole("alert"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/needs attention/i),
    ).not.toBeInTheDocument();
  });

  it("shows a warn banner listing every finding plus its remediation when not ok", () => {
    render(<FilesystemHealthBanner filesystem={unhealthy} t={t} />);

    const alarm = screen.getByRole("alert");
    expect(alarm).toHaveClass("warn");
    expect(alarm).toHaveTextContent(/needs attention/i);

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
  });
});
