import { describe, expect, it } from "vitest";

import type { ArchiveListItem, MeVault } from "@/api/types";
import {
  actionHint,
  availableActions,
  endpointForAction,
  type VaultCapabilities,
} from "@/pages/archive/actions";

const ownerCaps: VaultCapabilities = {
  role: "owner",
  can_operate: true,
  delete_enabled: true,
  cloud_deletion_enabled: true,
  is_vault_owner: true,
};

const operatorCaps: VaultCapabilities = {
  role: "operator",
  can_operate: true,
  delete_enabled: true,
  cloud_deletion_enabled: false,
  is_vault_owner: false,
};

const viewerCaps: VaultCapabilities = {
  role: "viewer",
  can_operate: false,
  delete_enabled: false,
  cloud_deletion_enabled: false,
  is_vault_owner: false,
};

const adminAsOwner: VaultCapabilities = {
  role: "owner",
  can_operate: true,
  delete_enabled: true,
  cloud_deletion_enabled: true,
  is_vault_owner: true,
};

const eligibleFile: ArchiveListItem = {
  type: "file",
  name: "readme.txt",
  path: "readme.txt",
  state: "both",
  local_exists: 1,
  cloud_exists: 1,
  storage_class: "STANDARD",
  upload_eligible: true,
  recover_eligible: true,
  cleanup_eligible: true,
};

const cloudOnlyFile: ArchiveListItem = {
  type: "file",
  name: "archive.pdf",
  path: "archive.pdf",
  state: "cloud_only",
  local_exists: 0,
  cloud_exists: 1,
  storage_class: "DEEP_ARCHIVE",
  upload_eligible: false,
  recover_eligible: true,
  cleanup_eligible: false,
};

const directory: ArchiveListItem = {
  type: "directory",
  name: "reports",
  path: "reports",
  item_count: 3,
  total_size: 1000,
  state: "mixed",
  available_actions: {
    upload: 2,
    recover: 1,
    "free-space": 3,
    "cloud-archive": 2,
    "cloud-purge": 2,
    "storage-class": 2,
  },
};

const localOnlyDirectory: ArchiveListItem = {
  type: "directory",
  name: "drafts",
  path: "drafts",
  item_count: 2,
  total_size: 100,
  state: "local_only",
  state_counts: { local_only: 2 },
  available_actions: { upload: 2, recover: 0, "free-space": 0 },
};

describe("availableActions — capability gating (seam 2)", () => {
  it("owner sees operate + storage-class/pin + cloud deletion actions when enabled", () => {
    expect(availableActions(eligibleFile, ownerCaps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
      "storage-class",
      "lifecycle-pin",
      "cloud-archive",
      "cloud-purge",
    ]);
    expect(availableActions(cloudOnlyFile, ownerCaps).map((a) => a.id)).toEqual([
      "recover",
      "storage-class",
      "lifecycle-pin",
      "cloud-archive",
      "cloud-purge",
    ]);
  });

  it("marks only permanent purge as danger; free-space stays default", () => {
    const actions = availableActions(eligibleFile, ownerCaps);
    expect(actions.find((a) => a.id === "free-space")?.tone).toBe("default");
    expect(actions.find((a) => a.id === "cloud-archive")?.tone).toBe("default");
    expect(actions.find((a) => a.id === "cloud-purge")?.tone).toBe("danger");
  });

  it("operator sees upload/recover/free-space/storage-class/pin but never cloud deletion", () => {
    expect(availableActions(eligibleFile, operatorCaps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
      "storage-class",
      "lifecycle-pin",
    ]);
  });

  it("viewer sees no row actions including storage-class", () => {
    expect(availableActions(eligibleFile, viewerCaps)).toEqual([]);
    expect(availableActions(cloudOnlyFile, viewerCaps)).toEqual([]);
  });

  it("admin with owner vault role matches owner capabilities", () => {
    expect(availableActions(eligibleFile, adminAsOwner).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
      "storage-class",
      "lifecycle-pin",
      "cloud-archive",
      "cloud-purge",
    ]);
  });

  it("hides free-space when delete_enabled is off", () => {
    const caps: VaultCapabilities = { ...ownerCaps, delete_enabled: false };
    expect(availableActions(eligibleFile, caps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "storage-class",
      "lifecycle-pin",
      "cloud-archive",
      "cloud-purge",
    ]);
    expect(
      availableActions(directory, caps).map((a) => a.id),
    ).not.toContain("free-space");
  });

  it("hides cloud-archive and cloud-purge when cloud_deletion_enabled is off", () => {
    const caps: VaultCapabilities = {
      ...ownerCaps,
      cloud_deletion_enabled: false,
    };
    expect(availableActions(eligibleFile, caps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
      "storage-class",
      "lifecycle-pin",
    ]);
    expect(availableActions(directory, caps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
      "storage-class",
      "lifecycle-pin",
    ]);
  });

  it("offers cloud deletion and storage-class on directories with cloud-bearing children", () => {
    expect(availableActions(directory, ownerCaps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
      "storage-class",
      "lifecycle-pin",
      "cloud-archive",
      "cloud-purge",
    ]);
    expect(
      availableActions(directory, ownerCaps).find((a) => a.id === "cloud-purge"),
    ).toMatchObject({ count: 2, tone: "danger" });
  });

  it("shows unpin when a path is already lifecycle-pinned", () => {
    const pinned: ArchiveListItem = { ...eligibleFile, lifecycle_pinned: true };
    expect(availableActions(pinned, operatorCaps).map((a) => a.id)).toContain(
      "lifecycle-unpin",
    );
    expect(availableActions(pinned, operatorCaps).map((a) => a.id)).not.toContain(
      "lifecycle-pin",
    );
  });

  it("hides cloud deletion on local-only directories", () => {
    expect(availableActions(localOnlyDirectory, ownerCaps).map((a) => a.id)).toEqual([
      "upload",
    ]);
  });

  it("falls back to state_counts when directory omits cloud action counts", () => {
    const legacyDir: ArchiveListItem = {
      type: "directory",
      name: "legacy",
      path: "legacy",
      item_count: 4,
      total_size: 10,
      state: "mixed",
      state_counts: { both: 1, cloud_only: 2, local_only: 1 },
      available_actions: { upload: 1, recover: 2, "free-space": 1 },
    };
    const purge = availableActions(legacyDir, ownerCaps).find(
      (a) => a.id === "cloud-purge",
    );
    expect(purge?.count).toBe(3);
  });
});

describe("actionHint — scope copy", () => {
  it("returns translated hints for deletion and storage-class actions", () => {
    const t = (key: string) =>
      ({
        "ui.row_action_free_space_hint": "local only",
        "ui.row_action_cloud_archive_hint": "hide cloud",
        "ui.row_action_cloud_purge_hint": "purge cloud",
        "ui.row_action_storage_class_hint": "change class",
      })[key] ?? key;
    expect(actionHint("free-space", t)).toBe("local only");
    expect(actionHint("cloud-archive", t)).toBe("hide cloud");
    expect(actionHint("cloud-purge", t)).toBe("purge cloud");
    expect(actionHint("storage-class", t)).toBe("change class");
  });
});

describe("endpointForAction — route mapping (seam 1 foundation)", () => {
  it("maps each action id to the matching POST path", () => {
    expect(endpointForAction("upload")).toBe("/api/upload");
    expect(endpointForAction("recover")).toBe("/api/recover");
    expect(endpointForAction("free-space")).toBe("/api/free-space");
    expect(endpointForAction("storage-class")).toBe("/api/storage-class");
    expect(endpointForAction("lifecycle-pin")).toBe("/api/lifecycle-pin");
    expect(endpointForAction("cloud-archive")).toBe("/api/cloud-archive");
    expect(endpointForAction("cloud-purge")).toBe("/api/cloud-purge");
  });
});

// Satisfy unused import in types for MeVault shape documentation.
void (null as unknown as MeVault);
