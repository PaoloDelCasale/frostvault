import type { StatsResponse } from "@/api/types";

/** Demo /api/stats payload used until auth + live queries land in later issues. */
export const demoStats: StatsResponse = {
  states: { both: 12, local_only: 3, cloud_only: 7 },
  storage: { local_bytes: 1536, cloud_bytes: 104857600 },
  active_jobs: 1,
  runtime: {},
  filesystem: {
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
    ],
  },
  delete_enabled: true,
};

export const demoMessages: Record<string, string> = {
  "state.both": "Server + cloud",
  "state.local_only": "Server only",
  "state.cloud_only": "Cloud only",
  "ui.server_space": "Server space",
  "ui.cloud_space": "Cloud space",
  "ui.active_operations": "Active operations",
  "ui.archive_subtitle":
    "Your files, on the server and safely stored in the cloud.",
  "ui.archive_statistics": "Archive statistics",
  "ui.filesystem_needs_attention": "Vault filesystem needs attention",
  "ui.filesystem_attention_detail":
    "Symbolic links and permission errors are reported; ownership and modes are never changed automatically.",
  "ui.file_list_placeholder": "File list",
  "ui.protected_archive": "Protected archive · {name}",
  "ui.protected_archive_detail":
    "Local cleanup is allowed only after the S3 copy has been verified.",
};

export function demoTranslate(
  key: string,
  params?: Record<string, string | number>,
): string {
  const template = demoMessages[key] ?? key;
  if (!params) return template;
  return Object.entries(params).reduce(
    (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
    template,
  );
}
