import { describe, expect, it } from "vitest";

import type { ArchiveListItem, MeVault } from "@/api/types";
import {
  actionHint,
  availableActions,
  endpointForAction,
  groupRowActions,
  partitionRowActions,
  type VaultCapabilities,
} from "@/pages/archive/actions";

const ownerCaps: VaultCapabilities = {
  delete_enabled: true,
  cloud_deletion_enabled: true,
};

const cloudDeletionOffCaps: VaultCapabilities = {
  delete_enabled: true,
  cloud_deletion_enabled: false,
};

const deletionOffCaps: VaultCapabilities = {
  delete_enabled: false,
  cloud_deletion_enabled: false,
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
    "cloud-purge": 0,

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
  it("owner sees copy and cloud deletion actions when enabled", () => {
    expect(availableActions(eligibleFile, ownerCaps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
    ]);
    expect(availableActions(cloudOnlyFile, ownerCaps).map((a) => a.id)).toEqual([
      "recover",
      "cloud-purge",
    ]);
  });

  it("marks only permanent purge as danger; free-space stays default", () => {
    const actions = availableActions(eligibleFile, ownerCaps);
    expect(actions.find((a) => a.id === "free-space")?.tone).toBe("default");
    expect(actions.map((a) => a.id)).not.toContain("cloud-archive");
    expect(availableActions(cloudOnlyFile, ownerCaps).find((a) => a.id === "cloud-purge")?.tone).toBe("danger");
  });

  it("without cloud deletion offers upload/recover/free-space only", () => {
    expect(availableActions(eligibleFile, cloudDeletionOffCaps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
    ]);
  });

  it("with every deletion disabled keeps only non-destructive actions", () => {
    expect(availableActions(eligibleFile, deletionOffCaps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
    ]);
  });

  it("hides free-space when delete_enabled is off", () => {
    const caps: VaultCapabilities = { ...ownerCaps, delete_enabled: false };
    expect(availableActions(eligibleFile, caps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
    ]);
    expect(
      availableActions(directory, caps).map((a) => a.id),
    ).not.toContain("free-space");
  });

  it("hides cloud-purge when cloud_deletion_enabled is off", () => {
    const caps: VaultCapabilities = {
      ...ownerCaps,
      cloud_deletion_enabled: false,
    };
    expect(availableActions(eligibleFile, caps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
    ]);
    expect(availableActions(directory, caps).map((a) => a.id)).toEqual([
      "upload",
      "recover",
      "free-space",
    ]);
  });

  it("offers cloud deletion only on wholly Cloud-only directories", () => {
    expect(availableActions(directory, ownerCaps).map((a) => a.id)).not.toContain("cloud-purge");
    expect(availableActions({ ...directory, available_actions: { "cloud-purge": 3 } }, ownerCaps))
      .toContainEqual({ id: "cloud-purge", count: 3, tone: "danger" });
  });

  it("hides cloud deletion on local-only directories", () => {
    expect(availableActions(localOnlyDirectory, ownerCaps).map((a) => a.id)).toEqual([
      "upload",
    ]);
  });

  it("never authorizes cloud deletion from protection state counts alone", () => {
    const legacyDir: ArchiveListItem = {
      type: "directory",
      name: "legacy",
      path: "legacy",
      item_count: 4,
      total_size: 10,
      state: "pending",
      state_counts: { protected: 1, cloud_only: 2, pending: 1 },
      available_actions: { upload: 1, recover: 2, "free-space": 1 },
    };
    const purge = availableActions(legacyDir, ownerCaps).find(
      (a) => a.id === "cloud-purge",
    );
    expect(purge).toBeUndefined();
  });
});

describe("partitionRowActions — essential vs overflow", () => {
  it("keeps upload/recover inline and groups the rest", () => {
    const { primary, overflow } = partitionRowActions(
      availableActions(eligibleFile, ownerCaps),
    );
    expect(primary.map((action) => action.id)).toEqual(["upload", "recover"]);
    expect(overflow.map((action) => action.id)).toEqual([
      "free-space",
    ]);
    expect(
      groupRowActions(overflow).map((group) => group.category),
    ).toEqual(["copies"]);
  });
});

describe("actionHint — scope copy", () => {
  it("returns translated hints for copy and deletion actions", () => {
    const t = (key: string) =>
      ({
        "ui.row_action_free_space_hint": "local only",
        "ui.row_action_cloud_purge_hint": "purge cloud",
      })[key] ?? key;
    expect(actionHint("free-space", t)).toBe("local only");
    expect(actionHint("cloud-purge", t)).toBe("purge cloud");
  });
});

describe("endpointForAction — route mapping (seam 1 foundation)", () => {
  it("maps each action id to the matching POST path", () => {
    expect(endpointForAction("upload")).toBe("/api/upload");
    expect(endpointForAction("recover")).toBe("/api/recover");
    expect(endpointForAction("free-space")).toBe("/api/free-space");
    expect(endpointForAction("cloud-purge")).toBe("/api/cloud-purge");
  });
});

// Satisfy unused import in types for MeVault shape documentation.
void (null as unknown as MeVault);
