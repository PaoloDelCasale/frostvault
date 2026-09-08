import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";

import {
  formatStorageClassRate,
  formatStorageClassRecovery,
  formatStorageClassRetrieval,
  type StorageClassOption,
} from "./storageClassOptions";

type Translate = (key: string, params?: Record<string, string | number>) => string;

function storageClassLabel(id: string, t: Translate): string {
  const labelKeys: Record<string, string> = {
    STANDARD: "ui.storage_class_name_standard",
    STANDARD_IA: "ui.storage_class_name_standard_ia",
    ONEZONE_IA: "ui.storage_class_name_onezone_ia",
    INTELLIGENT_TIERING: "ui.storage_class_name_intelligent_tiering",
    GLACIER_IR: "ui.storage_class_name_glacier_ir",
    GLACIER: "ui.storage_class_name_glacier",
    DEEP_ARCHIVE: "ui.storage_class_name_deep_archive",
  };
  const key = labelKeys[id] ?? `storage.${id}`;
  const translated = t(key);
  if (translated !== key) return translated;
  const legacyKey = `storage.${id}`;
  const legacy = t(legacyKey);
  return legacy === legacyKey ? id.replaceAll("_", " ") : legacy;
}

type OverlayCoords = {
  top: number;
  left: number;
  width: number;
  maxHeight: number;
};

function overlayCoordsFor(trigger: HTMLElement, contentHeight = 280): OverlayCoords {
  const rect = trigger.getBoundingClientRect();
  const gutter = 8;
  const gap = 4;
  const spaceBelow = window.innerHeight - rect.bottom - gutter;
  const spaceAbove = rect.top - gutter;
  const desiredHeight = Math.min(Math.max(contentHeight, 44), 360);
  const openUp = spaceBelow < desiredHeight && spaceAbove > spaceBelow;
  const availableWidth = Math.max(160, window.innerWidth - gutter * 2);
  const width = Math.min(Math.max(rect.width, 18 * 16), availableWidth);
  const availableHeight = openUp ? spaceAbove : spaceBelow;
  const maxHeight = Math.max(44, Math.min(360, availableHeight));
  const left = Math.min(
    Math.max(gutter, rect.right - width),
    window.innerWidth - width - gutter,
  );
  return {
    top: openUp
      ? Math.max(gutter, rect.top - gap - Math.min(desiredHeight, maxHeight))
      : rect.bottom + gap,
    left,
    width,
    maxHeight,
  };
}

export type StorageClassSelectProps = {
  id?: string;
  value: string;
  options: StorageClassOption[];
  onValueChange: (value: string) => void;
  t: Translate;
  "data-testid"?: string;
};

export function StorageClassSelect({
  id,
  value,
  options,
  onValueChange,
  t,
  "data-testid": testId = "storage-class-picker",
}: StorageClassSelectProps) {
  const listId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState<OverlayCoords | null>(null);
  const selected = options.find((option) => option.id === value) ?? options[0];

  useEffect(() => {
    if (!open) return;
    const trigger = rootRef.current?.querySelector("button");
    if (trigger) {
      setCoords(overlayCoordsFor(trigger, menuRef.current?.scrollHeight));
    }

    function onPointerDown(event: MouseEvent) {
      const target = event.target as Node;
      if (rootRef.current?.contains(target) || menuRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    }

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }

    function onReposition() {
      const next = rootRef.current?.querySelector("button");
      if (next) {
        setCoords(overlayCoordsFor(next, menuRef.current?.scrollHeight));
      }
    }

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onReposition);
    window.addEventListener("scroll", onReposition, true);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onReposition);
      window.removeEventListener("scroll", onReposition, true);
    };
  }, [open]);

  const list = open ? (
    <ul
      ref={menuRef}
      id={listId}
      role="listbox"
      aria-label={t("ui.storage_class_picker_label")}
      data-testid={`${testId}-menu`}
      className="fixed z-[80] overflow-auto rounded-[10px] border border-line bg-surface p-1 text-ink shadow-lg"
      style={
        coords
          ? {
              top: coords.top,
              left: coords.left,
              width: coords.width,
              maxHeight: coords.maxHeight,
            }
          : undefined
      }
    >
      {options.map((option) => {
        const isSelected = option.id === value;
        return (
          <li key={option.id} role="presentation">
            <button
              type="button"
              role="option"
              aria-selected={isSelected}
              className={cn(
                "flex min-h-11 w-full cursor-pointer items-center gap-2 rounded-lg px-3 text-left font-bold outline-none transition-colors duration-150 focus-visible:bg-canvas focus-visible:ring-2 focus-visible:ring-ring/40 motion-reduce:transition-none",
                isSelected
                  ? "bg-canvas text-green hover:bg-canvas"
                  : "text-ink hover:bg-green-soft/45",
              )}
              onClick={() => {
                onValueChange(option.id);
                setOpen(false);
              }}
            >
              <span className="flex size-4 shrink-0 items-center justify-center text-green">
                {isSelected ? <Check className="size-4" aria-hidden /> : null}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block font-bold">
                  {storageClassLabel(option.id, t)}
                </span>
                <span className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs font-medium leading-relaxed text-muted">
                  <span>{formatStorageClassRate(option, t)}</span>
                  <span>{formatStorageClassRetrieval(option, t)}</span>
                  <span>{formatStorageClassRecovery(option, t)}</span>
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  ) : null;

  return (
    <div ref={rootRef} className="relative">
      <button
        id={id}
        type="button"
        data-testid={testId}
        className={cn(
          "inline-flex min-h-11 w-full items-center justify-between gap-2 rounded-[10px] border border-input bg-surface px-3 font-bold text-ink",
          "text-left outline-none transition-[background-color,border-color,box-shadow] duration-150 hover:border-[var(--interactive-border-hover)] hover:bg-green-soft focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 motion-reduce:transition-none",
          open && "border-ring ring-3 ring-ring/50",
        )}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listId}
        aria-label={t("ui.storage_class_picker_label")}
        onClick={() => setOpen((next) => !next)}
      >
        <span className="min-w-0 flex-1">
          {selected ? (
            <span className="flex flex-col gap-0.5">
              <span className="font-bold tracking-wide">{storageClassLabel(selected.id, t)}</span>
              <span className="text-xs text-muted">
                {formatStorageClassRate(selected, t)}
                {" · "}
                {formatStorageClassRetrieval(selected, t)}
                {" · "}
                {formatStorageClassRecovery(selected, t)}
              </span>
            </span>
          ) : null}
        </span>
        <ChevronDown
          className={cn(
            "size-4 shrink-0 text-muted transition-transform",
            open && "rotate-180",
          )}
          aria-hidden
        />
      </button>
      {list && typeof document !== "undefined"
        ? createPortal(list, document.body)
        : list}
    </div>
  );
}
