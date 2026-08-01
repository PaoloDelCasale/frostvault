import { cn } from "@/lib/utils";

type ProgressBarProps = {
  value: number;
  label?: string;
  detail?: string;
  className?: string;
};

/** Progress track matching `.progress-track` / `.operation-progress`. */
export function ProgressBar({ value, label, detail, className }: ProgressBarProps) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <div className={cn("grid gap-1", className)} role="group" aria-label={label}>
      {label ? (
        <div className="flex justify-between gap-2 text-xs text-green">
          <span>{label}</span>
          <span>{Math.round(clamped)}%</span>
        </div>
      ) : null}
      <div
        className="h-[7px] w-full overflow-hidden rounded-badge bg-[var(--progress-track)]"
        role="progressbar"
        aria-valuenow={clamped}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label ?? "Progress"}
      >
        <span
          className="block h-full rounded-[inherit] bg-[linear-gradient(90deg,var(--progress-fill-end),var(--progress-fill-start))] transition-[width] duration-[450ms] ease-out"
          style={{ width: `${clamped}%` }}
        />
      </div>
      {detail ? <small className="text-[10px] text-muted">{detail}</small> : null}
    </div>
  );
}
