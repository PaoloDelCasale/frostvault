import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type CardProps = {
  children: ReactNode;
  className?: string;
};

/** Surface card — 14px radius from style.css `.card`. */
export function Card({ children, className }: CardProps) {
  return (
    <div
      className={cn(
        "rounded-card border border-line bg-surface p-4",
        className,
      )}
    >
      {children}
    </div>
  );
}
