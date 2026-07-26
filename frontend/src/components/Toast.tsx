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
        "backdrop-blur-[12px] shadow-[0_18px_45px_rgba(20,53,34,0.18),0_3px_10px_rgba(20,53,34,0.08)]",
        isError
          ? "border-[rgba(185,71,61,0.2)] bg-[rgba(255,249,248,0.98)] text-[#702b26]"
          : "border-[rgba(37,122,75,0.18)] bg-[rgba(247,255,250,0.97)] text-[#173d29]",
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "grid size-[38px] place-items-center rounded-xl text-lg font-black text-white",
          isError
            ? "bg-[linear-gradient(145deg,#d36a60,#b9473d)]"
            : "bg-[linear-gradient(145deg,#3b9a63,#257a4b)]",
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
