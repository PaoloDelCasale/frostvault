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
  "state.restoring": "Recovery in progress",
  "state.mixed": "Mixed state",
  "state.missing": "Unavailable",
  "state.filter.local_only": "Server only",
  "state.filter.both": "Server and cloud",
  "state.filter.cloud_only": "Cloud only",
  "state.filter.restoring": "Recovery in progress",
  "storage.STANDARD": "Standard",
  "storage.DEEP_ARCHIVE": "Deep Archive",
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
  "ui.search_placeholder": "Search by file or folder name…",
  "ui.filter_by_state": "Filter by state",
  "ui.all_items": "All items",
  "ui.go_up": "Go to parent folder",
  "ui.up": "↑ Up",
  "ui.name": "Name",
  "ui.size": "Size",
  "ui.state": "State",
  "ui.cloud_storage": "Cloud storage",
  "ui.previous": "Previous",
  "ui.next": "Next",
  "ui.page_label": "Page {page} of {pages} · {total} {unit}",
  "ui.items_unit": "items",
  "ui.files_found_unit": "files found",
  "ui.empty_no_files": "This folder is empty.",
  "ui.empty_no_matches": "No Vault Files match your search.",
  "ui.breadcrumb_archive": "Archive",
  "ui.folder_item_count": "{count} files in this folder",
  "ui.file_total": "File total",
  "ui.cloud_classes": "{count} cloud classes",
  "ui.more_actions": "More actions",
  "ui.path_history": "Path History",
  "ui.path_history_versions": "{count} Archive Versions",
  "ui.path_history_no_versions": "No Archive Versions",
  "ui.path_history_loading": "Loading Path History…",
  "ui.path_history_error": "Unable to load Path History.",
  "ui.close_path_history": "Close",
  "ui.symlink_rejected": "Symbolic link (rejected)",
  "ui.unsupported_local_entry": "Unsupported local entry",
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
