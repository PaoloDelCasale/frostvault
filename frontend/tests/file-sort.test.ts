import { describe, expect, it } from "vitest";

import type { ArchiveListItem } from "@/api/types";
import { compareArchiveItems } from "@/pages/archive/fileSort";

const file = (
  name: string,
  extras: Partial<ArchiveListItem> = {},
): ArchiveListItem =>
  ({
    type: "file",
    name,
    path: name,
    state: "both",
    ...extras,
  }) as ArchiveListItem;

describe("compareArchiveItems", () => {
  it("keeps directories before files", () => {
    const folder = {
      type: "directory",
      name: "z-folder",
      path: "z-folder",
      state: "both",
    } as ArchiveListItem;
    const item = file("a.txt");
    expect(compareArchiveItems(folder, item, "name", "asc")).toBeLessThan(0);
    expect(compareArchiveItems(folder, item, "name", "desc")).toBeLessThan(0);
  });

  it("sorts names with numeric awareness", () => {
    const items = [file("IMG_10.jpg"), file("IMG_2.jpg")];
    items.sort((a, b) => compareArchiveItems(a, b, "name", "asc"));
    expect(items.map((item) => item.name)).toEqual(["IMG_2.jpg", "IMG_10.jpg"]);
  });

  it("sorts by size", () => {
    const items = [
      file("big.bin", { local_size: 3000 }),
      file("small.bin", { local_size: 10 }),
    ];
    items.sort((a, b) => compareArchiveItems(a, b, "size", "desc"));
    expect(items.map((item) => item.name)).toEqual(["big.bin", "small.bin"]);
  });
});
