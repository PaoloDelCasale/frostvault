import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type PanelProps = {
  children: ReactNode;
  className?: string;
};

/** Panel container — 18px radius from style.css `.panel`. */
export function Panel({ children, className }: PanelProps) {
  return (
    <div
      className={cn(
        "overflow-hidden rounded-panel border border-line bg-surface",
        className,
      )}
    >
      {children}
    </div>
  );
}
