import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

type DatePickerProps = {
  id?: string;
  value: string;
  onValueChange: (value: string) => void;
  label: string;
  min?: string;
  max?: string;
};

type OverlayCoords = { top: number; left: number; width: number };

function parseDate(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

function isoDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function startOfMonth(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function addMonths(date: Date, amount: number): Date {
  return new Date(date.getFullYear(), date.getMonth() + amount, 1);
}

function daysInMonth(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
}

function overlayCoordsFor(trigger: HTMLElement): OverlayCoords {
  const rect = trigger.getBoundingClientRect();
  const gutter = 8;
  const gap = 4;
  const width = Math.min(304, window.innerWidth - gutter * 2);
  const height = 350;
  const spaceBelow = window.innerHeight - rect.bottom - gutter;
  const openUp = spaceBelow < height && rect.top > spaceBelow;
  return {
    top: openUp
      ? Math.max(gutter, rect.top - gap - height)
      : Math.min(rect.bottom + gap, window.innerHeight - gutter),
    left: Math.min(
      Math.max(gutter, rect.right - width),
      window.innerWidth - width - gutter,
    ),
    width,
  };
}

export function DatePicker({
  id,
  value,
  onValueChange,
  label,
  min,
  max,
}: DatePickerProps) {
  const { locale, t } = useI18n();
  const dialogId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const calendarRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState<OverlayCoords | null>(null);
  const selected = parseDate(value);
  const today = new Date();
  const [visibleMonth, setVisibleMonth] = useState(() =>
    startOfMonth(selected ?? today),
  );

  useEffect(() => {
    if (!open) return;
    setVisibleMonth(startOfMonth(parseDate(value) ?? new Date()));
    const trigger = rootRef.current?.querySelector("button");
    if (trigger) setCoords(overlayCoordsFor(trigger));

    function closeOutside(event: MouseEvent) {
      const target = event.target as Node;
      if (rootRef.current?.contains(target) || calendarRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    function reposition() {
      const next = rootRef.current?.querySelector("button");
      if (next) setCoords(overlayCoordsFor(next));
    }
    document.addEventListener("mousedown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      document.removeEventListener("mousedown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [open, value]);

  const firstDayOffset = (visibleMonth.getDay() + 6) % 7;
  const monthDays = daysInMonth(visibleMonth);
  const weekdays = Array.from({ length: 7 }, (_, index) => {
    const monday = new Date(2024, 0, 1 + index);
    return new Intl.DateTimeFormat(locale, { weekday: "narrow" }).format(monday);
  });
  const displayValue = selected
    ? new Intl.DateTimeFormat(locale, {
        day: "2-digit",
        month: "short",
        year: "numeric",
      }).format(selected)
    : t("date_picker.placeholder");
  const monthLabel = new Intl.DateTimeFormat(locale, {
    month: "long",
    year: "numeric",
  }).format(visibleMonth);
  const previousMonth = addMonths(visibleMonth, -1);
  const nextMonth = addMonths(visibleMonth, 1);
  const previousDisabled = Boolean(min && isoDate(new Date(
    previousMonth.getFullYear(),
    previousMonth.getMonth() + 1,
    0,
  )) < min);
  const nextDisabled = Boolean(max && isoDate(nextMonth) > max.slice(0, 7) + "-01");

  const calendar = open ? (
    <div
      ref={calendarRef}
      id={dialogId}
      role="dialog"
      aria-label={t("date_picker.calendar_for", { label })}
      className="fixed z-[90] rounded-xl border border-line bg-surface p-3 text-ink shadow-lg"
      style={coords ?? undefined}
    >
      <div className="flex items-center justify-between gap-2">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-9 min-h-9"
          disabled={previousDisabled}
          aria-label={t("date_picker.previous_month")}
          onClick={() => setVisibleMonth(previousMonth)}
        >
          <ChevronLeft aria-hidden="true" />
        </Button>
        <strong className="capitalize">{monthLabel}</strong>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-9 min-h-9"
          disabled={nextDisabled}
          aria-label={t("date_picker.next_month")}
          onClick={() => setVisibleMonth(nextMonth)}
        >
          <ChevronRight aria-hidden="true" />
        </Button>
      </div>
      <div className="mt-2 grid grid-cols-7 text-center text-xs font-bold text-muted" aria-hidden="true">
        {weekdays.map((weekday, index) => <span key={`${weekday}-${index}`}>{weekday}</span>)}
      </div>
      <div className="mt-1 grid grid-cols-7 gap-0.5">
        {Array.from({ length: firstDayOffset }, (_, index) => (
          <span key={`blank-${index}`} className="size-9" aria-hidden="true" />
        ))}
        {Array.from({ length: monthDays }, (_, index) => {
          const date = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth(), index + 1);
          const iso = isoDate(date);
          const isSelected = iso === value;
          const isToday = iso === isoDate(today);
          const disabled = Boolean((min && iso < min) || (max && iso > max));
          return (
            <button
              key={iso}
              type="button"
              disabled={disabled}
              aria-label={new Intl.DateTimeFormat(locale, { dateStyle: "long" }).format(date)}
              aria-pressed={isSelected}
              className={cn(
                "size-9 rounded-lg text-sm font-bold outline-none transition-colors hover:bg-green-soft focus-visible:ring-2 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-30 motion-reduce:transition-none",
                isSelected && "bg-green text-white hover:bg-green",
                !isSelected && isToday && "border border-green text-green",
              )}
              onClick={() => {
                onValueChange(iso);
                setOpen(false);
              }}
            >
              {index + 1}
            </button>
          );
        })}
      </div>
      <div className="mt-3 flex justify-between gap-2 border-t border-line pt-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={!value}
          onClick={() => {
            onValueChange("");
            setOpen(false);
          }}
        >
          {t("date_picker.clear")}
        </Button>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          disabled={Boolean((min && isoDate(today) < min) || (max && isoDate(today) > max))}
          onClick={() => {
            onValueChange(isoDate(today));
            setOpen(false);
          }}
        >
          {t("date_picker.today")}
        </Button>
      </div>
    </div>
  ) : null;

  return (
    <div ref={rootRef} className="relative">
      <button
        id={id}
        type="button"
        aria-label={label}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={dialogId}
        className={cn(
          "flex min-h-11 w-full items-center justify-between gap-3 rounded-[10px] border border-input bg-surface px-3.5 py-[11px] text-left font-bold text-ink outline-none",
          "transition-[background-color,border-color,box-shadow] duration-150 hover:border-[var(--interactive-border-hover)] hover:bg-green-soft focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 motion-reduce:transition-none",
          open && "border-ring ring-3 ring-ring/50",
          !value && "text-muted",
        )}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{displayValue}</span>
        <CalendarDays className="size-4 shrink-0 text-muted" aria-hidden="true" />
      </button>
      {calendar && typeof document !== "undefined"
        ? createPortal(calendar, document.body)
        : calendar}
    </div>
  );
}
