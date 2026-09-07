import type { ArchiveListItem } from "@/api/types";

import { itemSizeBytes } from "./fileLabels";

export type FileSortKey = "name" | "size";
export type FileSortOrder = "asc" | "desc";

export function parseFileSortKey(value: string | null): FileSortKey {
  return value === "size" ? "size" : "name";
}

export function parseFileSortOrder(value: string | null): FileSortOrder {
  return value === "desc" ? "desc" : "asc";
}

export function compareArchiveItems(
  left: ArchiveListItem,
  right: ArchiveListItem,
  key: FileSortKey,
  order: FileSortOrder,
): number {
  if (left.type !== right.type) {
    return left.type === "directory" ? -1 : 1;
  }
  const direction = order === "desc" ? -1 : 1;
  let cmp = 0;
  if (key === "size") {
    cmp = (itemSizeBytes(left) ?? 0) - (itemSizeBytes(right) ?? 0);
  } else {
    cmp = left.name.localeCompare(right.name, undefined, {
      numeric: true,
      sensitivity: "base",
    });
  }
  return cmp * direction;
}
