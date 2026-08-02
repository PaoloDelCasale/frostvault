import { cn } from "@/lib/utils";

type ToastProps = {
  open: boolean;
  message: string;
  variant?: "success" | "error";
  onClose: () => void;
};

export function Toast({
  open,
  message,
  variant = "success",
  onClose,
}: ToastProps) {
  if (!open) {
    return null;
  }

  const isError = variant === "error";

  return (
    <div
      role={isError ? "alert" : "status"}
      aria-live="polite"
      aria-atomic="true"
      className={cn(
        "fixed z-1000 grid min-h-[68px] grid-cols-[38px_minmax(0,1fr)_28px] items-center gap-[11px]",
        "top-[max(18px,env(safe-area-inset-top))] right-[max(18px,env(safe-area-inset-right))]",
        "w-[min(410px,calc(100vw-28px))] overflow-hidden rounded-2xl border px-3.5 py-3",
        "backdrop-blur-[12px] shadow-[0_18px_45px_var(--shadow-color),0_3px_10px_var(--shadow-color)]",
        isError
          ? "border-[var(--toast-error-border)] bg-[var(--toast-error-bg)] text-[var(--toast-error-fg)]"
          : "border-[var(--toast-success-border)] bg-[var(--toast-success-bg)] text-[var(--toast-success-fg)]",
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "grid size-[38px] place-items-center rounded-xl text-lg font-black text-white",
          isError
            ? "bg-[linear-gradient(145deg,var(--toast-error-start),var(--toast-error-end))]"
            : "bg-[linear-gradient(145deg,var(--toast-success-start),var(--toast-success-end))]",
        )}
      >
        {isError ? "!" : "✓"}
      </span>
      <span className="min-w-0 text-sm font-bold leading-snug [overflow-wrap:anywhere]">
        {message}
      </span>
      <button
        type="button"
        className="grid size-7 place-items-center rounded-lg bg-transparent p-0 text-[21px] leading-none opacity-55"
        aria-label="Close notification"
        onClick={onClose}
      >
        ×
      </button>
    </div>
  );
}
