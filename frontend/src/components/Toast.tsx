import { useEffect, useSyncExternalStore } from "react";

import { cn } from "@/lib/utils";
import {
  currentToasts,
  dismissToast,
  clearToasts,
  subscribeToasts,
  TOAST_SUCCESS_MS,
  type ToastVariant,
} from "./toastQueue";

type ToastProps = {
  message: string;
  variant?: ToastVariant;
  /** Translated accessible label of the dismiss button. */
  closeLabel: string;
  onClose: () => void;
};

/** Errors are alerts (assertive), successes are polite status messages. */
export function Toast({ message, variant = "success", closeLabel, onClose }: ToastProps) {
  const isError = variant === "error";

  return (
    <div
      role={isError ? "alert" : "status"}
      aria-atomic="true"
      className={cn(
        "fixed z-1000 grid min-h-[68px] grid-cols-[38px_minmax(0,1fr)_44px] items-center gap-[11px]",
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
        className="grid size-11 place-items-center rounded-lg bg-transparent p-0 text-[21px] leading-none opacity-55 transition-[background-color,opacity] duration-150 hover:bg-green-soft hover:opacity-100 focus-visible:bg-green-soft focus-visible:opacity-100 motion-reduce:transition-none"
        aria-label={closeLabel}
        onClick={onClose}
      >
        ×
      </button>
    </div>
  );
}

/** The single place where queued toasts appear; mounted once by the app shell. */
export function ToastRegion({ closeLabel }: { closeLabel: string }) {
  const items = useSyncExternalStore(subscribeToasts, currentToasts, currentToasts);
  const current = items[0];
  useEffect(() => {
    if (!current || current.variant !== "success") return;
    const timer = window.setTimeout(() => dismissToast(current.id), TOAST_SUCCESS_MS);
    return () => window.clearTimeout(timer);
  }, [current]);
  // The queue outlives a region only in tests; an unmounted shell drops it.
  useEffect(() => () => clearToasts(), []);
  if (!current) return null;
  return <Toast key={current.id} message={current.message} variant={current.variant} closeLabel={closeLabel} onClose={() => dismissToast(current.id)} />;
}
