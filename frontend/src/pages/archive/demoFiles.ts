/**
 * Dev-only mock of /api/files and /api/file-history for 375px screenshots.
 * Activated when window.__FV_DEMO_FILES__ is set before React mounts,
 * or when ?demo=files is present (patched in at screenshot time via evaluate).
 */
import type { FileHistoryResponse, FilesResponse } from "@/api/types";

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
      recoverable_version_count: 1,
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

export function installDemoFilesFetch(): void {
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
    if (url.includes("/api/file-history")) {
      return new Response(JSON.stringify(demoFileHistory), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/api/files")) {
      const params = new URL(url, window.location.origin).searchParams;
      const q = params.get("q") || "";
      const directory = params.get("directory") || "";
      let body: FilesResponse = demoRootListing;
      if (q) body = demoSearchListing;
      else if (directory.startsWith("reports")) body = demoNestedListing;
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/api/me") || url.includes("/api/i18n") || url.includes("/api/vaults") || url.includes("/api/stats")) {
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return realFetch(input, init);
  };
}
