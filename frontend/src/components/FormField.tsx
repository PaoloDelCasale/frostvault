import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

type FormFieldProps = {
  label: string;
  htmlFor: string;
  help?: string;
  children: ReactNode;
  className?: string;
};

export function FormField({ label, htmlFor, help, children, className }: FormFieldProps) {
  return (
    <label htmlFor={htmlFor} className={cn("grid gap-1.5 text-[13px] font-bold text-muted", className)}>
      <span>{label}</span>
      {children}
      {help ? <span className="font-medium text-muted">{help}</span> : null}
    </label>
  );
}

type TextInputProps = InputHTMLAttributes<HTMLInputElement>;

export function TextInput({ className, ...props }: TextInputProps) {
  return (
    <input
      className={cn(
        "min-h-11 w-full rounded-[10px] border border-input bg-white px-3.5 py-[11px] text-ink",
        className,
      )}
      {...props}
    />
  );
}

type TextSelectProps = SelectHTMLAttributes<HTMLSelectElement>;

export function TextSelect({ className, ...props }: FormSelectProps) {
  return (
    <select
      className={cn(
        "min-h-11 w-full rounded-[10px] border border-input bg-white px-3.5 py-[11px] text-ink",
        className,
      )}
      {...props}
    />
  );
}
