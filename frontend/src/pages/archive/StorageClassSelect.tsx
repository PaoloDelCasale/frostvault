import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";

import {
  formatStorageClassRate,
  formatStorageClassRecovery,
  formatStorageClassRetrieval,
  type StorageClassOption,
} from "./storageClassOptions";

type Translate = (key: string, params?: Record<string, string | number>) => string;

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
  const [open, setOpen] = useState(false);
  const selected = options.find((option) => option.id === value) ?? options[0];

  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent) {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={rootRef} className="relative mt-1">
      <button
        id={id}
        type="button"
        data-testid={testId}
        className={cn(
          "flex w-full items-center justify-between gap-3 rounded-lg border border-line bg-surface px-3 py-2.5",
          "text-left text-sm text-ink outline-none",
          "hover:bg-canvas focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
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
              <span className="font-semibold tracking-wide">{selected.id}</span>
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
          className={cn("size-4 shrink-0 text-muted transition-transform", open && "rotate-180")}
          aria-hidden
        />
      </button>

      {open ? (
        <ul
          id={listId}
          role="listbox"
          aria-label={t("ui.storage_class_picker_label")}
          data-testid={`${testId}-menu`}
          className={cn(
            "absolute z-[80] mt-1.5 max-h-80 w-full overflow-auto",
            "rounded-lg border border-line bg-surface p-1.5 text-ink shadow-lg",
          )}
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
                    "flex w-full cursor-pointer items-start gap-3 rounded-md px-3 py-3 text-left outline-none",
                    "hover:bg-canvas focus-visible:bg-canvas focus-visible:ring-2 focus-visible:ring-ring/40",
                    isSelected && "bg-green-soft/60",
                  )}
                  onClick={() => {
                    onValueChange(option.id);
                    setOpen(false);
                  }}
                >
                  <span className="mt-0.5 flex size-4 shrink-0 items-center justify-center text-green">
                    {isSelected ? <Check className="size-4" aria-hidden /> : null}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block font-semibold tracking-wide text-ink">
                      {option.id}
                    </span>
                    <span className="mt-1.5 grid gap-1 text-xs leading-relaxed text-muted sm:grid-cols-2 sm:gap-x-4">
                      <span>{formatStorageClassRate(option, t)}</span>
                      <span>{formatStorageClassRetrieval(option, t)}</span>
                      <span className="sm:col-span-2">
                        {formatStorageClassRecovery(option, t)}
                      </span>
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
