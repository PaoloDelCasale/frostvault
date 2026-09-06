import type { ArchiveListItem } from "@/api/types";

export type FileKind =
  | "folder"
  | "pdf"
  | "image"
  | "video"
  | "audio"
  | "archive"
  | "spreadsheet"
  | "presentation"
  | "code"
  | "text"
  | "file";

const KIND_BY_EXTENSION: Record<string, FileKind> = {
  pdf: "pdf",
  jpg: "image",
  jpeg: "image",
  png: "image",
  gif: "image",
  webp: "image",
  svg: "image",
  bmp: "image",
  tif: "image",
  tiff: "image",
  heic: "image",
  heif: "image",
  avif: "image",
  ico: "image",
  mp4: "video",
  mov: "video",
  mkv: "video",
  avi: "video",
  webm: "video",
  m4v: "video",
  wmv: "video",
  mp3: "audio",
  wav: "audio",
  flac: "audio",
  aac: "audio",
  ogg: "audio",
  m4a: "audio",
  zip: "archive",
  tar: "archive",
  gz: "archive",
  tgz: "archive",
  bz2: "archive",
  "7z": "archive",
  rar: "archive",
  xz: "archive",
  xls: "spreadsheet",
  xlsx: "spreadsheet",
  csv: "spreadsheet",
  ods: "spreadsheet",
  tsv: "spreadsheet",
  ppt: "presentation",
  pptx: "presentation",
  odp: "presentation",
  key: "presentation",
  js: "code",
  ts: "code",
  tsx: "code",
  jsx: "code",
  py: "code",
  go: "code",
  rs: "code",
  java: "code",
  c: "code",
  cpp: "code",
  h: "code",
  cs: "code",
  php: "code",
  html: "code",
  css: "code",
  json: "code",
  sh: "code",
  txt: "text",
  md: "text",
  rtf: "text",
  log: "text",
  doc: "text",
  docx: "text",
  odt: "text",
};

export function extensionOf(name: string): string {
  const base = name.split("/").pop() ?? name;
  const dot = base.lastIndexOf(".");
  if (dot <= 0 || dot === base.length - 1) return "";
  return base.slice(dot + 1).toLowerCase();
}

export function fileKindForItem(item: Pick<ArchiveListItem, "type" | "name">): FileKind {
  if (item.type === "directory") return "folder";
  return KIND_BY_EXTENSION[extensionOf(item.name)] ?? "file";
}
