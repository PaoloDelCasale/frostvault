import type { BadgeState } from "@/components/Badge";
import type { StorageClass } from "@/components/StorageBadge";
import type { ArchiveListItem, DirectoryListItem, VaultFileListItem } from "@/api/types";

type Translate = (key: string, params?: Record<string, string | number>) => string;

/** Resolve Badge state + label for a Vault File or directory aggregate. */
export function itemStateBadge(
  item: ArchiveListItem,
  t: Translate,
): { state: BadgeState; label: string } {
  if (item.type === "file") {
    if (item.local_file_type === "symlink") {
      return { state: "unsupported", label: t("ui.symlink_rejected") };
    }
    if (
      item.local_file_type &&
      item.local_file_type !== "regular" &&
      item.local_exists
    ) {
      return { state: "unsupported", label: t("ui.unsupported_local_entry") };
    }
  }
  const raw = item.state;
  const known: BadgeState[] = [
    "both",
    "local_only",
    "cloud_only",
    "restoring",
    "mixed",
    "missing",
    "unsupported",
  ];
  const state = (known.includes(raw as BadgeState) ? raw : "missing") as BadgeState;
  const key = `state.${state}`;
  const label = t(key);
  return { state, label: label === key ? state : label };
}

export function itemSizeBytes(item: ArchiveListItem): number | null | undefined {
  if (item.type === "directory") {
    return item.total_size;
  }
  return item.local_size ?? item.cloud_size;
}

export function storageKind(storageClass: string | null | undefined): StorageClass {
  if (!storageClass) return "standard";
  if (storageClass === "DEEP_ARCHIVE") return "deep-archive";
  if (storageClass.includes("GLACIER")) return "glacier";
  return "standard";
}

export function storageLabel(
  storageClass: string | null | undefined,
  t: Translate,
): string {
  if (!storageClass) return "—";
  const key = `storage.${storageClass}`;
  const label = t(key);
  return label === key ? storageClass.replaceAll("_", " ") : label;
}

/** Cloud storage cell/card content for a list item. */
export function cloudStorageDisplay(
  item: ArchiveListItem,
  t: Translate,
):
  | { kind: "none"; text: string }
  | { kind: "summary"; text: string }
  | { kind: "badge"; storage: StorageClass; label: string } {
  if (item.type === "directory") {
    if (!item.storage_class) {
      if ((item.storage_class_count ?? 0) > 1) {
        return {
          kind: "summary",
          text: t("ui.cloud_classes", { count: item.storage_class_count ?? 0 }),
        };
      }
      return { kind: "none", text: "—" };
    }
    return {
      kind: "badge",
      storage: storageKind(item.storage_class),
      label: storageLabel(item.storage_class, t),
    };
  }
  if (!item.cloud_exists) {
    return { kind: "none", text: "—" };
  }
  const storageClass = item.storage_class || "STANDARD";
  return {
    kind: "badge",
    storage: storageKind(storageClass),
    label: storageLabel(storageClass, t),
  };
}

export function isDirectory(item: ArchiveListItem): item is DirectoryListItem {
  return item.type === "directory";
}

export function isVaultFile(item: ArchiveListItem): item is VaultFileListItem {
  return item.type === "file";
}

export function buildBreadcrumbs(
  directory: string,
  archiveLabel: string,
): Array<{ name: string; path: string }> {
  const crumbs: Array<{ name: string; path: string }> = [
    { name: archiveLabel, path: "" },
  ];
  if (!directory) return crumbs;
  let path = "";
  for (const name of directory.split("/")) {
    path = path ? `${path}/${name}` : name;
    crumbs.push({ name, path });
  }
  return crumbs;
}

/**
 * Collapse deep breadcrumbs for narrow viewports: keep root, ellipsis, and
 * the last two segments so the trail never forces horizontal scroll.
 */
export function collapseBreadcrumbs<T extends { name: string; path: string }>(
  crumbs: T[],
  maxVisible = 4,
): Array<T | { name: "…"; path: null; ellipsis: true }> {
  if (crumbs.length <= maxVisible) {
    return crumbs;
  }
  const head = crumbs[0]!;
  const tail = crumbs.slice(-(maxVisible - 2));
  return [head, { name: "…" as const, path: null, ellipsis: true as const }, ...tail];
}

export function isBreadcrumbEllipsis(
  crumb: { name: string; path: string | null; ellipsis?: boolean },
): crumb is { name: "…"; path: null; ellipsis: true } {
  return crumb.ellipsis === true;
}

export function parentDirectory(directory: string): string {
  if (!directory) return "";
  const parts = directory.split("/");
  parts.pop();
  return parts.join("/");
}
