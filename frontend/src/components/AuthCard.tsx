import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type AuthCardProps = {
  children: ReactNode;
  className?: string;
};

/** Auth surface — 22px radius (`.auth-card`). */
export function AuthCard({ children, className }: AuthCardProps) {
  return (
    <section
      className={cn(
        "rounded-auth border border-line bg-surface p-[30px]",
        "shadow-[0_18px_50px_var(--shadow-color)]",
        className,
      )}
    >
      {children}
    </section>
  );
}
