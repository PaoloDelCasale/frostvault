/**
 * Dev-only in-memory API for frontend work and 375px screenshots.
 *
 * Activate with `?demo=files` (screenshots) or `?demo=1` (interactive playground)
 * in Vite development, or an explicit `VITE_ALLOW_DEMO=1` build.
 *
 * Edit the exported listings and the `seedDemoState()` fixtures below to change
 * the mock catalog, users, vaults, and notifications. Mutations (upload,
 * recover, free-space, storage-class, admin user create, …) update this
 * in-memory state so the SPA stays interactive without a backend.
 */
import { DEMO_MODE_ENABLED, getDemoSearchParam } from "@/demoGate";
import {
  compareArchiveItems,
  parseFileSortKey,
  parseFileSortOrder,
} from "./fileSort";
import type {
  FileHistoryResponse,
  FilesResponse,
  JobGroup,
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
      available_actions: {
        upload: 1,
        recover: 0,
        "free-space": 2,
        "storage-class": 2,
      },
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
      lifecycle_pinned: true,
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
  aggregate_status: "ready",
};

const demoNestedListing: FilesResponse = {
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
      local_file_type: "regular",
      cloud_exists: 0,
      cloud_size: null,
      storage_class: null,
      state: "local_only",
      upload_eligible: true,
      recover_eligible: false,
      cleanup_eligible: false,
    },
  ],
  total: 2,
  page: 1,
  directory: "reports",
  mode: "browse",
  aggregate_status: "ready",
};

const demoYearListing: FilesResponse = {
  items: [
    {
      type: "file",
      name: "q1-report.pdf",
      path: "reports/2024/q1-report.pdf",
      local_exists: 1,
      local_size: 2048,
      cloud_exists: 1,
      cloud_size: 2048,
      storage_class: "STANDARD",
      state: "both",
    },
    {
      type: "file",
      name: "q2-report.pdf",
      path: "reports/2024/q2-report.pdf",
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
  directory: "reports/2024",
  mode: "browse",
  aggregate_status: "ready",
};

const demoSearchListing: FilesResponse = {
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
  aggregate_status: "ready",
};

const DEMO_PHOTO_COUNT = 250;

function buildDemoPhotosListing(): FilesResponse {
  const items = Array.from({ length: DEMO_PHOTO_COUNT }, (_, index) => {
    const n = index + 1;
    const padded = String(n).padStart(3, "0");
    const slot = n % 3;
    if (slot === 1) {
      return {
        type: "file" as const,
        name: `IMG_${padded}.jpg`,
        path: `photos/IMG_${padded}.jpg`,
        local_exists: 1,
        local_size: 2_048_000,
        cloud_exists: 1,
        cloud_size: 2_048_000,
        storage_class: "STANDARD",
        state: "both" as const,
      };
    }
    if (slot === 2) {
      return {
        type: "file" as const,
        name: `IMG_${padded}.png`,
        path: `photos/IMG_${padded}.png`,
        local_exists: 1,
        local_size: 1_024_000,
        local_file_type: "regular",
        cloud_exists: 0,
        cloud_size: null,
        storage_class: null,
        state: "local_only" as const,
        upload_eligible: true,
        recover_eligible: false,
        cleanup_eligible: false,
      };
    }
    return {
      type: "file" as const,
      name: `IMG_${padded}.heic`,
      path: `photos/IMG_${padded}.heic`,
      local_exists: 0,
      local_size: null,
      cloud_exists: 1,
      cloud_size: 3_072_000,
      storage_class: "GLACIER",
      state: "cloud_only" as const,
      upload_eligible: false,
      recover_eligible: true,
      cleanup_eligible: false,
    };
  });
  return {
    items,
    total: items.length,
    page: 1,
    directory: "photos",
    mode: "browse",
    aggregate_status: "ready",
  };
}

const demoPhotosListing = buildDemoPhotosListing();

const demoPhotosFolder = {
  type: "directory" as const,
  name: "photos",
  path: "photos",
  item_count: DEMO_PHOTO_COUNT,
  total_size: demoPhotosListing.items.reduce(
    (sum, item) => sum + Number(item.local_size ?? item.cloud_size ?? 0),
    0,
  ),
  state: "mixed" as const,
  state_counts: {
    both: Math.ceil(DEMO_PHOTO_COUNT / 3),
    local_only: Math.floor(DEMO_PHOTO_COUNT / 3),
    cloud_only: Math.floor(DEMO_PHOTO_COUNT / 3),
  },
  storage_class: null,
  storage_class_count: 2,
  available_actions: {
    upload: Math.floor(DEMO_PHOTO_COUNT / 3),
    recover: Math.floor(DEMO_PHOTO_COUNT / 3),
    "free-space": Math.ceil(DEMO_PHOTO_COUNT / 3),
    "storage-class": Math.ceil(DEMO_PHOTO_COUNT / 3),
  },
};

const demoFileHistory: FileHistoryResponse = {
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

const demoActiveJobs: JobsResponse = {
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

type DemoNotification = {
  id: number;
  user_id: number;
  vault_id: number | null;
  job_id: number | null;
  event: string;
  title: string;
  body: string;
  title_key: string | null;
  body_key: string | null;
  message_params: Record<string, unknown>;
  in_app_enabled: boolean;
  dedupe_key: string | null;
  created_at: string;
  read: boolean;
  read_at: string | null;
};

type DemoUser = {
  id: number;
  display_name: string;
  username: string;
  active: boolean;
  is_admin: boolean;
  vault_count: number;
  has_password: boolean;
  identity_count: number;
};

type DemoVault = {
  id: number;
  name: string;
  slug: string;
  source_root: string;
  s3_prefix: string;
  enabled: boolean;
  member_count: number;
  encryption_mode: string;
};

type DemoMember = {
  id: number;
  username: string;
  display_name: string;
  role: string;
};

type DemoState = {
  root: FilesResponse;
  nested: FilesResponse;
  year: FilesResponse;
  photos: FilesResponse;
  search: FilesResponse;
  jobs: JobGroup[];
  jobSeq: number;
  notifications: DemoNotification[];
  users: DemoUser[];
  vaults: DemoVault[];
  members: DemoMember[];
  locale: string;
};

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function seedDemoState(): DemoState {
  return {
    root: {
      ...clone(demoRootListing),
      items: [
        clone(demoRootListing.items[0]!),
        clone(demoPhotosFolder),
        ...clone(demoRootListing.items.slice(1)),
      ],
      total: demoRootListing.total + 1,
    },
    nested: clone(demoNestedListing),
    year: clone(demoYearListing),
    photos: clone(demoPhotosListing),
    search: clone(demoSearchListing),
    jobs: [],
    jobSeq: 1,
    notifications: [
      {
        id: 101,
        user_id: 1,
        vault_id: 1,
        job_id: null,
        event: "upload.completed",
        title: "Upload finished",
        body: "readme.txt is now in the cloud archive.",
        title_key: null,
        body_key: null,
        message_params: {},
        in_app_enabled: true,
        dedupe_key: "upload-readme",
        created_at: "2026-03-01T09:15:00Z",
        read: false,
        read_at: null,
      },
      {
        id: 102,
        user_id: 1,
        vault_id: 1,
        job_id: null,
        event: "scan.completed",
        title: "Catalog scan complete",
        body: "Test Archive catalog is up to date.",
        title_key: null,
        body_key: null,
        message_params: {},
        in_app_enabled: true,
        dedupe_key: "scan-1",
        created_at: "2026-03-01T08:00:00Z",
        read: true,
        read_at: "2026-03-01T08:05:00Z",
      },
    ],
    users: [
      {
        id: 1,
        display_name: "Local Admin",
        username: "admin",
        active: true,
        is_admin: true,
        vault_count: 1,
        has_password: true,
        identity_count: 0,
      },
      {
        id: 2,
        display_name: "Alex Operator",
        username: "alex",
        active: true,
        is_admin: false,
        vault_count: 1,
        has_password: true,
        identity_count: 1,
      },
    ],
    vaults: [
      {
        id: 1,
        name: "Test Archive",
        slug: "test",
        source_root: "/sources/test",
        s3_prefix: "development/test",
        enabled: true,
        member_count: 2,
        encryption_mode: "plain",
      },
    ],
    members: [
      {
        id: 1,
        username: "admin",
        display_name: "Local Admin",
        role: "owner",
      },
      {
        id: 2,
        username: "alex",
        display_name: "Alex Operator",
        role: "operator",
      },
    ],
    locale: "en",
  };
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function readJson(init?: RequestInit): Record<string, unknown> {
  const body = init?.body;
  if (typeof body !== "string" || !body) return {};
  try {
    return JSON.parse(body) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function parseUrl(url: string): { path: string; search: URLSearchParams } {
  const parsed = new URL(url, "http://demo.local");
  return { path: parsed.pathname, search: parsed.searchParams };
}

function allListings(state: DemoState): FilesResponse[] {
  return [state.root, state.nested, state.year, state.photos, state.search];
}

function findItem(state: DemoState, path: string) {
  for (const listing of allListings(state)) {
    const item = listing.items.find((entry) => entry.path === path);
    if (item) return item;
  }
  return undefined;
}

function applyFileAction(
  state: DemoState,
  action: string,
  path: string,
  extra: Record<string, unknown> = {},
): void {
  const item = findItem(state, path);
  if (!item || item.type !== "file") return;

  if (action === "upload") {
    item.cloud_exists = 1;
    item.cloud_size = item.local_size ?? item.cloud_size ?? 1024;
    item.state = "both";
    item.upload_eligible = false;
    item.cleanup_eligible = true;
    item.storage_class = item.storage_class ?? "STANDARD";
  } else if (action === "recover") {
    item.local_exists = 1;
    item.local_size = item.cloud_size ?? item.local_size ?? 1024;
    item.local_file_type = "regular";
    item.state = "both";
    item.recover_eligible = false;
    item.cleanup_eligible = true;
  } else if (action === "free-space") {
    item.local_exists = 0;
    item.local_size = null;
    item.local_file_type = null;
    item.state = "cloud_only";
    item.cleanup_eligible = false;
    item.recover_eligible = true;
  } else if (action === "storage-class") {
    const nextClass = String(extra.target_storage_class || item.storage_class || "STANDARD");
    item.storage_class = nextClass;
    if (extra.pin_after) item.lifecycle_pinned = true;
  } else if (action === "lifecycle-pin") {
    item.lifecycle_pinned = true;
  } else if (action === "lifecycle-unpin") {
    item.lifecycle_pinned = false;
  } else if (action === "cloud-archive") {
    item.state = "cloud_only";
    item.local_exists = 0;
    item.local_size = null;
  }
}

function jobStatusFor(action: string): string {
  if (action === "upload") return "uploading";
  if (action === "recover") return "restoring";
  if (action === "free-space") return "running";
  return "running";
}

function enqueueJob(
  state: DemoState,
  action: string,
  path: string,
  extra: Record<string, unknown> = {},
): { group_id: string; message: string; job_ids: number[] } {
  state.jobSeq += 1;
  const id = `demo-job-${state.jobSeq}`;
  const group: JobGroup = {
    id,
    path,
    action,
    status: jobStatusFor(action),
    percent: 18,
    total_bytes: 2048,
    transferred_bytes: 360,
    item_count: 1,
    completed_count: 0,
    failed_count: 0,
    cancelled_count: 0,
    message_key:
      action === "storage-class" ? "job.storage_class_changing" : undefined,
  };
  state.jobs.push(group);
  window.setTimeout(() => {
    const current = state.jobs.find((job) => job.id === id);
    if (!current || current.status === "cancelled") return;
    current.percent = 100;
    current.status = "completed";
    current.completed_count = 1;
    current.transferred_bytes = current.total_bytes;
    applyFileAction(state, action, path, extra);
  }, 1200);
  window.setTimeout(() => {
    state.jobs = state.jobs.filter((job) => job.id !== id);
  }, 2800);
  return { group_id: id, message: "started", job_ids: [state.jobSeq] };
}

function countByState(items: FilesResponse["items"]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const item of items) {
    const key = String(item.state ?? "");
    if (!key) continue;
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return counts;
}

function countByStorage(items: FilesResponse["items"]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const item of items) {
    const key = String(item.storage_class ?? "") || "none";
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return counts;
}

function matchesStorage(
  item: FilesResponse["items"][number],
  storage: string,
): boolean {
  if (!storage) return true;
  const storageClass = String(item.storage_class ?? "");
  if (storage === "none") return !storageClass;
  return storageClass === storage;
}

function listingFor(
  state: DemoState,
  directory: string,
  q: string,
  fileState: string,
  page = 1,
  pageSize = 100,
  sort = "name",
  order = "asc",
  storage = "",
): FilesResponse {
  let body: FilesResponse = state.root;
  if (q) body = state.search;
  else if (directory === "reports/2024") body = state.year;
  else if (directory === "reports") body = state.nested;
  else if (directory === "photos") body = state.photos;
  else if (directory) {
    return {
      items: [],
      total: 0,
      page: 1,
      directory,
      mode: "browse",
      aggregate_status: "ready",
      state_counts: {},
      storage_counts: {},
    };
  }
  const scoped = body.items;
  const stateCounts = countByState(
    storage ? scoped.filter((item) => matchesStorage(item, storage)) : scoped,
  );
  const storageCounts = countByStorage(
    fileState ? scoped.filter((item) => item.state === fileState) : scoped,
  );
  const filtered = scoped.filter((item) => {
    if (fileState && item.state !== fileState) return false;
    return matchesStorage(item, storage);
  });
  const sortKey = parseFileSortKey(sort);
  const sortOrder = parseFileSortOrder(order);
  const sorted = [...filtered].sort((left, right) =>
    compareArchiveItems(left, right, sortKey, sortOrder),
  );
  const size = Number.isFinite(pageSize) && pageSize > 0 ? pageSize : 100;
  const current = Number.isFinite(page) && page > 0 ? page : 1;
  const start = (current - 1) * size;
  return {
    ...body,
    items: sorted.slice(start, start + size),
    total: sorted.length,
    page: current,
    directory: body.directory,
    state_counts: stateCounts,
    storage_counts: storageCounts,
  };
}

function mePayload(state: DemoState) {
  const vault = state.vaults[0];
  return {
    id: 1,
    username: "admin",
    display_name: "Local Admin",
    is_admin: true,
    active: true,
    session_version: 1,
    csrf_token: "demo",
    offline_cache_generation: "demo-session-vault-1",
    auth_method: "local",
    locale: state.locale,
    locales: ["en", "it"],
    vault: vault
      ? {
          id: vault.id,
          slug: vault.slug,
          name: vault.name,
          role: "owner",
          can_operate: true,
          delete_enabled: true,
          cloud_deletion_enabled: true,
          is_vault_owner: true,
        }
      : null,
  };
}

function statsPayload(state: DemoState, screenshotJobs: boolean) {
  const files = [
    ...state.root.items,
    ...state.nested.items,
    ...state.year.items,
    ...state.photos.items,
  ].filter(
    (item) => item.type === "file",
  );
  const states: Record<string, number> = {};
  let localBytes = 0;
  let cloudBytes = 0;
  for (const item of files) {
    const key = String(item.state ?? "missing");
    states[key] = (states[key] ?? 0) + 1;
    localBytes += Number(item.local_size ?? 0);
    cloudBytes += Number(item.cloud_size ?? 0);
  }
  return {
    states,
    storage: { local_bytes: localBytes, cloud_bytes: cloudBytes },
    active_jobs: screenshotJobs ? 1 : state.jobs.filter((job) => job.status !== "completed").length,
    runtime: {},
    filesystem: null,
    delete_enabled: true,
  };
}

const storageClasses = {
  currency: "EUR",
  items: [
    {
      id: "STANDARD",
      currency: "EUR",
      storage_rate_eur_per_gib_month: 0.023,
      retrieval: "instant",
      min_duration_days: 0,
      requires_restore: false,
      availability_zones: "multi",
    },
    {
      id: "STANDARD_IA",
      currency: "EUR",
      storage_rate_eur_per_gib_month: 0.0125,
      retrieval: "instant",
      min_duration_days: 30,
      requires_restore: false,
      availability_zones: "multi",
      retrieval_rate_eur_per_gib: 0.01,
    },
    {
      id: "GLACIER",
      currency: "EUR",
      storage_rate_eur_per_gib_month: 0.004,
      retrieval: "restore",
      min_duration_days: 90,
      requires_restore: true,
      availability_zones: "multi",
      restore_hours_bulk: 12,
      restore_hours_standard: 5,
      restore_rate_eur_per_gib_bulk: 0.0025,
      restore_rate_eur_per_gib_standard: 0.01,
    },
    {
      id: "DEEP_ARCHIVE",
      currency: "EUR",
      storage_rate_eur_per_gib_month: 0.00099,
      retrieval: "restore",
      min_duration_days: 180,
      requires_restore: true,
      availability_zones: "multi",
      restore_hours_bulk: 48,
      restore_hours_standard: 12,
      restore_rate_eur_per_gib_bulk: 0.0025,
      restore_rate_eur_per_gib_standard: 0.02,
    },
  ],
};

function installDemoEventSource(): void {
  if (typeof window === "undefined" || typeof window.EventSource !== "function") {
    return;
  }
  if ((window.EventSource as unknown as { __frostvaultDemo?: boolean }).__frostvaultDemo) {
    return;
  }

  class DemoEventSource {
    url: string;
    readyState = 1;
    withCredentials = true;
    onerror: ((ev: Event) => unknown) | null = null;
    onmessage: ((ev: MessageEvent<string>) => unknown) | null = null;
    onopen: ((ev: Event) => unknown) | null = null;

    constructor(url: string) {
      this.url = String(url);
    }

    addEventListener(): void {
      return;
    }

    removeEventListener(): void {
      return;
    }

    close(): void {
      this.readyState = 2;
    }
  }

  (DemoEventSource as unknown as { __frostvaultDemo: boolean }).__frostvaultDemo = true;
  window.EventSource = DemoEventSource as unknown as typeof EventSource;
}

export function installDemoFilesFetch(): void {
  if (!DEMO_MODE_ENABLED || typeof window === "undefined") return;

  const realFetch = window.fetch.bind(window);
  const state = seedDemoState();
  let catalogPromise: Promise<Record<string, string>> | null = null;
  installDemoEventSource();

  async function englishCatalog(): Promise<Record<string, string>> {
    if (!catalogPromise) {
      catalogPromise = realFetch("/demo-en-catalog.json")
        .then(async (response) => {
          if (!response.ok) return {};
          return (await response.json()) as Record<string, string>;
        })
        .catch(() => ({}));
    }
    return catalogPromise;
  }

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url,
    );
    const method = (init?.method ?? "GET").toUpperCase();
    const { path, search } = parseUrl(url);
    const body = readJson(init);
    const showJob =
      getDemoSearchParam("job") === "1" ||
      getDemoSearchParam("job") === "storage-class";
    const jobAction =
      getDemoSearchParam("job") === "storage-class"
        ? "storage-class"
        : "upload";

    if (!path.startsWith("/api/") && path !== "/login") {
      return realFetch(input, init);
    }

    if (path === "/api/jobs" && method === "GET") {
      if (showJob) {
        return json({
          items: [],
          groups: [
            {
              ...demoActiveJobs.groups[0],
              action: jobAction,
              path: jobAction === "storage-class" ? "archive.pdf" : "reports",
              status: "uploading",
              message_key:
                jobAction === "storage-class"
                  ? "job.storage_class_changing"
                  : undefined,
            },
          ],
        });
      }
      return json({ items: [], groups: state.jobs });
    }

    if (path === "/api/files/versions") {
      return json({
        path: search.get("path") || "archive.pdf",
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

    if (path === "/api/recover/estimate") {
      return json({
        path: String(body.path ?? "archive.pdf"),
        archive_version_id: String(body.archive_version_id ?? "ver-new"),
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

    if (path === "/api/vault/cloud-deletion") {
      return json({
        enabled: true,
        purge_delay_seconds: 60,
        delete_marker_explanation: "Delete markers hide the current key.",
        generated_phrase: "PURGE-PHRASE",
      });
    }

    if (path === "/api/cloud-deletion/preview") {
      return json({
        object_count: 1,
        version_count: 2,
        delete_marker_count: 1,
        byte_count: 2048,
      });
    }

    if (path === "/api/file-history") {
      const historyPath = search.get("path") || "readme.txt";
      return json({ ...demoFileHistory, path: historyPath });
    }

    if (path === "/api/files") {
      return json(
        listingFor(
          state,
          search.get("directory") || "",
          search.get("q") || "",
          search.get("state") || "",
          Number(search.get("page") || "1"),
          Number(search.get("page_size") || "100"),
          search.get("sort") || "name",
          search.get("order") || "asc",
          search.get("storage") || "",
        ),
      );
    }

    if (path === "/api/i18n/catalog" || path.startsWith("/api/i18n")) {
      const messages = await englishCatalog();
      const locale = search.get("locale") || state.locale;
      return json({
        locale: locale === "it" ? "it" : "en",
        locales: ["en", "it"],
        messages,
      });
    }

    if (path === "/api/locale" && method === "PUT") {
      state.locale = String(body.locale ?? state.locale);
      return json({ locale: state.locale });
    }

    if (path === "/api/me") {
      return json(mePayload(state));
    }

    if (path === "/api/vaults" && method === "GET") {
      return json({
        items: state.vaults.map((vault) => ({
          id: vault.id,
          slug: vault.slug,
          name: vault.name,
          role: "owner",
        })),
      });
    }

    if (path === "/api/vaults/select" && method === "POST") {
      return json({ message: "ok", vault_id: body.vault_id ?? 1 });
    }

    if (path === "/api/stats") {
      return json(statsPayload(state, showJob));
    }

    if (path === "/api/storage-classes") {
      return json(storageClasses);
    }

    if (path === "/api/rename-candidates") {
      return json({ items: [] });
    }

    if (path === "/api/catalog/revision") {
      return json({
        vault_id: 1,
        revision: 1,
        domains: [],
        has_gap: false,
        changed: false,
      });
    }

    if (path === "/api/notifications") {
      const status = search.get("status") || "unread";
      const items = state.notifications.filter((item) => {
        if (status === "unread") return !item.read;
        if (status === "read") return item.read;
        return true;
      });
      return json({
        items,
        unread_count: state.notifications.filter((item) => !item.read).length,
        has_more: false,
      });
    }

    if (path === "/api/notifications/read" && method === "POST") {
      const id = Number(body.notification_id);
      const item = state.notifications.find((entry) => entry.id === id);
      if (item) {
        item.read = true;
        item.read_at = new Date().toISOString();
      }
      return json(item ?? { id, read: true });
    }

    if (path === "/api/notifications/read-all" && method === "POST") {
      let marked = 0;
      for (const item of state.notifications) {
        if (!item.read) {
          item.read = true;
          item.read_at = new Date().toISOString();
          marked += 1;
        }
      }
      return json({ marked_count: marked, unread_count: 0 });
    }

    if (path === "/api/vault/notification-preferences") {
      if (method === "POST") {
        return json({
          id: 1,
          user_id: 1,
          vault_id: 1,
          event: body.event ?? "upload.completed",
          in_app_enabled: body.in_app_enabled ?? true,
          push_enabled: body.push_enabled ?? false,
        });
      }
      return json({ items: [] });
    }

    if (path === "/api/push/config") {
      return json({ configured: false, vapid_public_key: null });
    }

    if (path === "/api/vault/members") {
      if (method === "POST") {
        const userId = Number(body.user_id);
        const user = state.users.find((entry) => entry.id === userId);
        const role = String(body.role ?? "viewer");
        if (user && !state.members.some((member) => member.id === user.id)) {
          state.members.push({
            id: user.id,
            username: user.username,
            display_name: user.display_name,
            role,
          });
        }
        return json({ message: "ok" });
      }
      return json({ items: state.members });
    }

    if (path.startsWith("/api/vault/members/") && method === "DELETE") {
      const userId = Number(path.split("/").pop());
      state.members = state.members.filter((member) => member.id !== userId);
      return json({ message: "ok" });
    }

    if (path === "/api/vault/user-lookup" && method === "POST") {
      const username = String(body.username ?? "");
      const user = state.users.find((entry) => entry.username === username);
      if (!user) return json({ detail: "User not found" }, 404);
      const member = state.members.find((entry) => entry.id === user.id);
      return json({
        id: user.id,
        username: user.username,
        display_name: user.display_name,
        current_vault_role: member?.role ?? null,
      });
    }

    if (path === "/api/vault/quotas") {
      return json({
        limits: {},
        usage: {},
        evaluation: { state: "evaluated", allowed: true, decisions: [] },
      });
    }

    if (path === "/api/vault/lifecycle" || path === "/api/lifecycle") {
      return json({
        default_policy_id: null,
        folder_overrides: [],
        policies: [],
        guided_profiles: {
          standard_only: { transitions: [] },
          ia_after_30: {
            transitions: [{ days: 30, storage_class: "STANDARD_IA" }],
          },
          archive_tiered: {
            transitions: [
              { days: 30, storage_class: "STANDARD_IA" },
              { days: 90, storage_class: "GLACIER" },
            ],
          },
        },
      });
    }

    if (path === "/api/vault/operation-policy" || path === "/api/operation-policy") {
      return json({
        auto_upload: true,
        auto_local_cleanup: false,
        local_retention_days: 45,
        stability_seconds: 300,
        include_globs: [],
        exclude_globs: [],
        bandwidth_limit_kibps: null,
        operating_windows: [],
      });
    }

    if (path === "/api/audit-events") {
      return json({ items: [] });
    }

    if (path === "/api/admin/users" && method === "GET") {
      return json({ items: state.users });
    }

    if (path === "/api/admin/users" && method === "POST") {
      const id = Math.max(0, ...state.users.map((user) => user.id)) + 1;
      const user: DemoUser = {
        id,
        display_name: String(body.display_name ?? `User ${id}`),
        username: String(body.username ?? `user-${id}`),
        active: true,
        is_admin: Boolean(body.is_admin),
        vault_count: 0,
        has_password: body.password != null && String(body.password).length > 0,
        identity_count: 0,
      };
      state.users.push(user);
      return json(user);
    }

    const adminUserMatch = path.match(/^\/api\/admin\/users\/(\d+)$/);
    if (adminUserMatch && method === "PATCH") {
      const user = state.users.find((entry) => entry.id === Number(adminUserMatch[1]));
      if (!user) return json({ detail: "User not found" }, 404);
      if (typeof body.display_name === "string") user.display_name = body.display_name;
      if (typeof body.active === "boolean") user.active = body.active;
      if (typeof body.is_admin === "boolean") user.is_admin = body.is_admin;
      if (body.password) user.has_password = true;
      return json(user);
    }

    if (path.endsWith("/identities") && path.includes("/api/admin/users/")) {
      return json({ items: [] });
    }

    if (path === "/api/admin/vaults" && method === "GET") {
      return json({ items: state.vaults });
    }

    if (path === "/api/admin/vaults" && method === "POST") {
      const id = Math.max(0, ...state.vaults.map((vault) => vault.id)) + 1;
      const vault: DemoVault = {
        id,
        name: String(body.name ?? `Vault ${id}`),
        slug: String(body.slug ?? `vault-${id}`),
        source_root: "/sources/managed/demo",
        s3_prefix: `development/vault-${id}`,
        enabled: true,
        member_count: 1,
        encryption_mode: String(body.encryption_mode ?? "plain"),
      };
      state.vaults.push(vault);
      return json(vault);
    }

    if (path === "/api/admin/source-volumes") {
      return json({
        items: [
          {
            alias: "test",
            path: "/sources/test",
            access: "rw",
            health: "ok",
            vault_count: 1,
            source_area_count: 1,
            diagnostic: null,
          },
        ],
      });
    }

    if (path.includes("/browse") && path.includes("source-volumes")) {
      return json({
        volume_alias: "test",
        path: search.get("path") || "",
        parent_path: null,
        entries: [
          {
            name: "inbox",
            path: "inbox",
            type: "directory",
            occupation: null,
          },
        ],
      });
    }

    if (path === "/api/admin/invites") {
      if (method === "POST") {
        return json({ token: "demo-invite-token" });
      }
      return json({ items: [] });
    }

    if (path === "/api/admin/settings") {
      return json({
        revision: 1,
        groups: {
          security: [],
          oidc: [],
          operations: [
            {
              key: "scan_interval",
              environment_variable: "SCAN_INTERVAL_SECONDS",
              source: "environment_default",
              mutability: "runtime_managed",
              restart_required: false,
              effective_value: 1800,
              minimum: 30,
              maximum: 86400,
            },
          ],
          restore: [],
          vault_defaults: [],
        },
      });
    }

    if (path === "/api/admin/oidc-configuration") {
      return json({
        enabled: false,
        issuer: "",
        client_id: "",
        client_secret_configured: false,
        scopes: ["openid"],
        login_transaction_ttl_seconds: 300,
        callback_url: "http://127.0.0.1:5173/auth/oidc/callback",
        source: "environment",
        draft: null,
        configuration_status: "disabled",
        last_validation: null,
        active: {
          enabled: false,
          issuer: "",
          client_id: "",
          client_secret_configured: false,
          scopes: ["openid"],
          login_transaction_ttl_seconds: 300,
          callback_url: "http://127.0.0.1:5173/auth/oidc/callback",
          source: "environment",
        },
      });
    }

    if (path === "/api/admin/audit-events" || path === "/api/admin/worker-errors") {
      return json({ items: [] });
    }

    if (path === "/api/admin/metadata-backups") {
      return json({ items: [], status: { last_run: null, last_error: null } });
    }

    if (path === "/api/admin/cost-price-books") {
      return json({ items: [] });
    }

    if (path === "/api/admin/source-areas" || path === "/api/source-areas") {
      return json({ items: [] });
    }

    if (path === "/api/login" && method === "POST") {
      return json({ message: "ok" });
    }

    if (path === "/api/logout" && method === "POST") {
      return json({ message: "Signed out", message_key: "login.signed_out" });
    }

    if (path === "/api/jobs/cancel" || path === "/api/upload/cancel") {
      const groupId = String(body.group_id ?? "");
      const job = state.jobs.find((entry) => entry.id === groupId);
      if (job) {
        job.status = "cancelled";
        job.cancelled_count = 1;
      }
      return json({ message: "cancelled", group_id: groupId });
    }

    if (path === "/api/lifecycle-pin" && (method === "PUT" || method === "POST")) {
      const pinPath = String(body.path ?? "");
      const pinned = Boolean(body.pinned);
      applyFileAction(
        state,
        pinned ? "lifecycle-pin" : "lifecycle-unpin",
        pinPath,
      );
      return json({ message: "ok", pinned, path: pinPath });
    }

    const mutatingActions = new Set([
      "/api/upload",
      "/api/recover",
      "/api/free-space",
      "/api/cloud-archive",
      "/api/storage-class",
      "/api/cloud-purge",
      "/api/recover/approve",
      "/api/cloud-purge/accelerate",
      "/api/scan",
    ]);
    if (method !== "GET" && mutatingActions.has(path)) {
      const action = path.split("/").pop() || "upload";
      const targetPath = String(body.path ?? "readme.txt");
      if (action === "approve" || action === "accelerate" || action === "scan") {
        return json({ group_id: "demo", message: "started", job_ids: [1] });
      }
      return json(enqueueJob(state, action, targetPath, body));
    }

    if (method !== "GET" && path.startsWith("/api/")) {
      return json({ message: "ok", group_id: "demo", job_ids: [1] });
    }

    if (path.startsWith("/api/")) {
      return json({ items: [] });
    }

    return realFetch(input, init);
  };
}
