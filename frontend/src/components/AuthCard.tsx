import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type AuthCardProps = {
  children: ReactNode;
  className?: string;
};

/** Auth surface — 22px radius from style.css `.auth-card`. */
export function AuthCard({ children, className }: AuthCardProps) {
  return (
    <section
      className={cn(
        "rounded-auth border border-line bg-white p-[30px]",
        "shadow-[0_18px_50px_rgba(22,45,32,0.08)]",
        className,
      )}
    >
      {children}
    </section>
  );
}
