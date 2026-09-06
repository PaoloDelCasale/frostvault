import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";

export type MenuSelectOption = {
  value: string;
  label: string;
};

export type MenuSelectProps = {
  value: string;
  options: MenuSelectOption[];
  onValueChange: (value: string) => void;
  label: string;
  className?: string;
  triggerClassName?: string;
  /** `overlay` drops a menu over following content; `inline` expands in flow. */
  placement?: "overlay" | "inline";
  "data-testid"?: string;
};

type OverlayCoords = {
  top: number;
  left: number;
  width: number;
  maxHeight: number;
};

function overlayCoordsFor(trigger: HTMLElement): OverlayCoords {
  const rect = trigger.getBoundingClientRect();
  const gutter = 8;
  const spaceBelow = window.innerHeight - rect.bottom - gutter;
  const spaceAbove = rect.top - gutter;
  const openUp = spaceBelow < 220 && spaceAbove > spaceBelow;
  const maxHeight = Math.max(160, openUp ? spaceAbove : spaceBelow);
  const width = Math.max(rect.width, 16 * 16);
  const left = Math.min(
    Math.max(gutter, rect.right - width),
    window.innerWidth - width - gutter,
  );
  return {
    top: openUp ? rect.top - 4 - Math.min(maxHeight, spaceAbove) : rect.bottom + 4,
    left,
    width,
    maxHeight,
  };
}

/**
 * Themed combobox that replaces native `<select>` popups in the shell and
 * archive toolbar. Keyboard: click / Enter / Space to open, Escape to close.
 */
export function MenuSelect({
  value,
  options,
  onValueChange,
  label,
  className,
  triggerClassName,
  placement = "overlay",
  "data-testid": testId,
}: MenuSelectProps) {
  const listId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState<OverlayCoords | null>(null);
  const selected = options.find((option) => option.value === value);

  useEffect(() => {
    if (!open) return;
    const trigger = rootRef.current?.querySelector("button");
    if (placement === "overlay" && trigger) {
      setCoords(overlayCoordsFor(trigger));
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
      if (placement === "overlay" && next) setCoords(overlayCoordsFor(next));
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
  }, [open, placement]);

  const list = open ? (
    <ul
      ref={menuRef}
      id={listId}
      role="listbox"
      aria-label={label}
      className={cn(
        "z-[80] overflow-auto rounded-[10px] border border-line bg-surface p-1 text-ink shadow-lg",
        placement === "overlay" ? "fixed" : "mt-1",
      )}
      style={
        placement === "overlay" && coords
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
        const isSelected = option.value === value;
        return (
          <li key={option.value} role="presentation">
            <button
              type="button"
              role="option"
              aria-selected={isSelected}
              className={cn(
                "flex min-h-11 w-full cursor-pointer items-center gap-2 rounded-lg px-3 text-left font-bold outline-none",
                "focus-visible:bg-canvas focus-visible:ring-2 focus-visible:ring-ring/40",
                isSelected ? "bg-canvas text-green" : "text-ink",
              )}
              onClick={() => {
                onValueChange(option.value);
                setOpen(false);
              }}
            >
              <span className="flex size-4 shrink-0 items-center justify-center text-green">
                {isSelected ? <Check className="size-4" aria-hidden /> : null}
              </span>
              <span className="min-w-0 truncate">{option.label}</span>
            </button>
          </li>
        );
      })}
    </ul>
  ) : null;

  return (
    <div ref={rootRef} className={cn("relative", className)}>
      <button
        type="button"
        role="combobox"
        aria-label={label}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-controls={listId}
        data-testid={testId}
        className={cn(
          "inline-flex min-h-11 w-full items-center justify-between gap-2 rounded-[10px] border border-input bg-surface px-3 font-bold text-ink",
          "outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
          open && "border-ring ring-3 ring-ring/50",
          triggerClassName,
        )}
        onClick={() => setOpen((next) => !next)}
      >
        <span className="min-w-0 truncate">{selected?.label ?? label}</span>
        <ChevronDown
          className={cn(
            "size-4 shrink-0 text-muted transition-transform",
            open && "rotate-180",
          )}
          aria-hidden
        />
      </button>
      {placement === "overlay" && list && typeof document !== "undefined"
        ? createPortal(list, document.body)
        : list}
    </div>
  );
}
