/**
 * Dev-only mock of /api/files, /api/file-history, /api/jobs and operation
 * endpoints for 375px screenshots.
 * Activated when ?demo=files is present (patched in at screenshot time).
 */
import type {
  FileHistoryResponse,
  FilesResponse,
  JobsResponse,
} from "@/api/types";

export const demoRootListing: FilesResponse = {
  items: [
    {
      type: "directory",
      name: "reports",
      path: "reports",
      item_count: 3,
      total_size: 1536,
      local_size: 1024,
      cloud_size: 512,
      state: "mixed",
      state_counts: { both: 2, local_only: 1 },
      storage_class: null,
      storage_class_count: 2,
      available_actions: { upload: 1, recover: 0, "free-space": 2 },
    },
    {
      type: "file",
      name: "readme.txt",
      path: "readme.txt",
      local_exists: 1,
      local_size: 1024,
      local_file_type: "regular",
      cloud_exists: 1,
      cloud_size: 1024,
      storage_class: "STANDARD",
      state: "both",
      upload_eligible: false,
      recover_eligible: false,
      cleanup_eligible: true,
      recoverable_version_count: 1,
    },
    {
      type: "file",
      name: "archive.pdf",
      path: "archive.pdf",
      local_exists: 0,
      local_size: null,
      local_file_type: null,
      cloud_exists: 1,
      cloud_size: 2048,
      storage_class: "DEEP_ARCHIVE",
      state: "cloud_only",
      upload_eligible: false,
      recover_eligible: true,
      cleanup_eligible: false,
      recoverable_version_count: 2,
    },
  ],
  total: 3,
  page: 1,
  directory: "",
  mode: "browse",
};

export const demoNestedListing: FilesResponse = {
  items: [
    {
      type: "directory",
      name: "2024",
      path: "reports/2024",
      item_count: 2,
      total_size: 4096,
      state: "both",
      state_counts: { both: 2 },
      storage_class: "STANDARD",
      storage_class_count: 1,
    },
    {
      type: "file",
      name: "q1-summary.pdf",
      path: "reports/q1-summary.pdf",
      local_exists: 1,
      local_size: 2048,
      cloud_exists: 1,
      cloud_size: 2048,
      storage_class: "STANDARD",
      state: "both",
    },
  ],
  total: 2,
  page: 1,
  directory: "reports",
  mode: "browse",
};

export const demoSearchListing: FilesResponse = {
  items: [
    {
      type: "file",
      name: "lease.pdf",
      path: "docs/contracts/lease.pdf",
      local_exists: 1,
      local_size: 8192,
      cloud_exists: 1,
      cloud_size: 8192,
      storage_class: "GLACIER",
      state: "both",
    },
  ],
  total: 1,
  page: 1,
  directory: "",
  mode: "search",
};

export const demoFileHistory: FileHistoryResponse = {
  vault_file_id: "vf-readme",
  path: "readme.txt",
  path_history: [
    { path: "docs/old-readme.txt", valid_from: "2024-01-01T00:00:00Z" },
    { path: "readme.txt", valid_from: "2024-06-01T00:00:00Z" },
  ],
  versions: [
    { object_key: "vault/readme.txt" },
    { object_key: "vault/docs/old-readme.txt" },
  ],
};

export const demoActiveJobs: JobsResponse = {
  items: [],
  groups: [
    {
      id: "demo-job-1",
      path: "reports",
      action: "upload",
      status: "uploading",
      percent: 42,
      total_bytes: 1024,
      transferred_bytes: 430,
      item_count: 1,
      completed_count: 0,
      failed_count: 0,
      cancelled_count: 0,
    },
  ],
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function installDemoFilesFetch(): void {
  const realFetch = window.fetch.bind(window);

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url,
    );
    const method = (init?.method ?? "GET").toUpperCase();
    const showJob =
      new URLSearchParams(window.location.search).get("job") === "1";

    if (url.includes("/api/jobs") && method === "GET") {
      return json(showJob ? demoActiveJobs : { items: [], groups: [] });
    }
    if (url.includes("/api/files/versions")) {
      return json({
        path: "archive.pdf",
        items: [
          {
            id: "ver-old",
            version_number: 1,
            storage_class: "STANDARD",
            size: 100,
            recoverable: true,
            created_at: "2024-01-01T00:00:00Z",
          },
          {
            id: "ver-new",
            version_number: 2,
            storage_class: "DEEP_ARCHIVE",
            size: 2048,
            recoverable: true,
            created_at: "2025-06-01T12:00:00Z",
          },
        ],
        recoverable_count: 2,
        default_archive_version_id: "ver-new",
        supported_restore_tiers: ["Standard", "Bulk"],
        default_restore_tier: "Standard",
        default_restore_days: 7,
      });
    }
    if (url.includes("/api/recover/estimate")) {
      return json({
        path: "archive.pdf",
        archive_version_id: "ver-new",
        storage_class: "DEEP_ARCHIVE",
        requires_restore: true,
        restore_object_irreversible: true,
        high_impact: false,
        estimate: {
          tier: "Standard",
          days: 7,
          estimated_cost_eur: 1.25,
          estimated_hours: 12,
        },
      });
    }
    if (url.includes("/api/vault/cloud-deletion")) {
      return json({
        enabled: true,
        purge_delay_seconds: 60,
        delete_marker_explanation: "Delete markers hide the current key.",
        generated_phrase: "PURGE-PHRASE",
      });
    }
    if (url.includes("/api/cloud-deletion/preview")) {
      return json({
        object_count: 1,
        version_count: 2,
        delete_marker_count: 1,
        byte_count: 2048,
      });
    }
    if (url.includes("/api/file-history")) {
      return json(demoFileHistory);
    }
    if (url.includes("/api/files")) {
      const search = new URL(url, window.location.origin).searchParams;
      const q = search.get("q") || "";
      const directory = search.get("directory") || "";
      let body: FilesResponse = demoRootListing;
      if (q) body = demoSearchListing;
      else if (directory.startsWith("reports")) body = demoNestedListing;
      return json(body);
    }
    if (url.includes("/api/i18n")) {
      return json({
        locale: "en",
        locales: ["en", "it"],
        messages: {},
      });
    }
    if (url.includes("/api/me")) {
      return json({
        id: 1,
        username: "admin",
        display_name: "Local Admin",
        is_admin: true,
        active: true,
        session_version: 1,
        csrf_token: "demo",
        auth_method: "local",
        locale: "en",
        locales: ["en", "it"],
        vault: {
          id: 1,
          slug: "test",
          name: "Test Archive",
          role: "owner",
          can_operate: true,
          delete_enabled: true,
          cloud_deletion_enabled: true,
          is_vault_owner: true,
        },
      });
    }
    if (url.includes("/api/vaults")) {
      return json({ items: [] });
    }
    if (url.includes("/api/stats")) {
      return json({
        states: {},
        storage: { local_bytes: 0, cloud_bytes: 0 },
        active_jobs: new URLSearchParams(window.location.search).get("job") === "1" ? 1 : 0,
        runtime: {},
        filesystem: null,
        delete_enabled: true,
      });
    }
    if (method === "POST") {
      return json({ group_id: "demo", message: "started", job_ids: [1] });
    }
    return realFetch(input, init);
  };
}
