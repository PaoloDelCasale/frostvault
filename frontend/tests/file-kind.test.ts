import { describe, expect, it } from "vitest";

import { extensionOf, fileKindForItem } from "@/pages/archive/fileKind";

describe("fileKindForItem", () => {
  it("treats directories as folders regardless of name", () => {
    expect(
      fileKindForItem({ type: "directory", name: "photos.jpg" }),
    ).toBe("folder");
  });

  it("maps common extensions to a descriptive kind", () => {
    expect(fileKindForItem({ type: "file", name: "lease.pdf" })).toBe("pdf");
    expect(fileKindForItem({ type: "file", name: "q1-summary.PDF" })).toBe("pdf");
    expect(fileKindForItem({ type: "file", name: "shot.heic" })).toBe("image");
    expect(fileKindForItem({ type: "file", name: "clip.mp4" })).toBe("video");
    expect(fileKindForItem({ type: "file", name: "take.wav" })).toBe("audio");
    expect(fileKindForItem({ type: "file", name: "bundle.zip" })).toBe("archive");
    expect(fileKindForItem({ type: "file", name: "budget.xlsx" })).toBe(
      "spreadsheet",
    );
    expect(fileKindForItem({ type: "file", name: "deck.pptx" })).toBe(
      "presentation",
    );
    expect(fileKindForItem({ type: "file", name: "main.ts" })).toBe("code");
    expect(fileKindForItem({ type: "file", name: "notes.txt" })).toBe("text");
    expect(fileKindForItem({ type: "file", name: "readme" })).toBe("file");
  });

  it("ignores leading dots and missing extensions", () => {
    expect(extensionOf(".env")).toBe("");
    expect(extensionOf("archive.")).toBe("");
    expect(fileKindForItem({ type: "file", name: ".gitignore" })).toBe("file");
  });
});
