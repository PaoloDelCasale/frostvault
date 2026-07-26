import { describe, expect, it } from "vitest";

import type { ArchiveListItem, MeVault } from "@/api/types";
import {
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
  available_actions: { upload: 2, recover: 1, "free-space": 3 },
};

describe("availableActions — capability gating (seam 2)", () => {
  it("owner sees operate + cloud deletion actions when enabled", () => {
    expect(availableActions(eligibleFile, ownerCaps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
      "cloud-archive",
      "cloud-purge",
    ]);
    expect(availableActions(cloudOnlyFile, ownerCaps).map((a) => a.id)).toEqual([
      "recover",
      "cloud-archive",
      "cloud-purge",
    ]);
  });

  it("operator sees upload/recover/free-space but never cloud deletion", () => {
    expect(availableActions(eligibleFile, operatorCaps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
    ]);
  });

  it("viewer sees no row actions", () => {
    expect(availableActions(eligibleFile, viewerCaps)).toEqual([]);
    expect(availableActions(cloudOnlyFile, viewerCaps)).toEqual([]);
  });

  it("admin with owner vault role matches owner capabilities", () => {
    expect(availableActions(eligibleFile, adminAsOwner).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
      "cloud-archive",
      "cloud-purge",
    ]);
  });

  it("hides free-space when delete_enabled is off", () => {
    const caps: VaultCapabilities = { ...ownerCaps, delete_enabled: false };
    expect(availableActions(eligibleFile, caps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
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
    ]);
  });
});

describe("endpointForAction — route mapping (seam 1 foundation)", () => {
  it("maps each action id to the matching POST path", () => {
    expect(endpointForAction("upload")).toBe("/api/upload");
    expect(endpointForAction("recover")).toBe("/api/recover");
    expect(endpointForAction("free-space")).toBe("/api/free-space");
    expect(endpointForAction("cloud-archive")).toBe("/api/cloud-archive");
    expect(endpointForAction("cloud-purge")).toBe("/api/cloud-purge");
  });
});

// Satisfy unused import in types for MeVault shape documentation.
void (null as unknown as MeVault);
