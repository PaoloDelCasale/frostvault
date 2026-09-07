import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

type FormFieldProps = {
  label: string;
  htmlFor: string;
  help?: string;
  children: ReactNode;
  className?: string;
  /** Use a non-activating text heading for composite controls such as custom menus. */
  labelAsText?: boolean;
};

export function FormField({
  label,
  htmlFor,
  help,
  children,
  className,
  labelAsText = false,
}: FormFieldProps) {
  const content = (
    <>
      <span>{label}</span>
      {children}
      {help ? <span className="font-medium text-muted">{help}</span> : null}
    </>
  );
  const fieldClassName = cn(
    "grid gap-1.5 text-[13px] font-bold text-muted",
    className,
  );

  return labelAsText ? (
    <div className={fieldClassName}>{content}</div>
  ) : (
    <label htmlFor={htmlFor} className={fieldClassName}>{content}</label>
  );
}

type FormInputProps = InputHTMLAttributes<HTMLInputElement>;

export function FormInput({ className, ...props }: FormInputProps) {
  return (
    <input
      className={cn(
        "min-h-11 w-full rounded-[10px] border border-input bg-surface px-3.5 py-[11px] text-ink",
        className,
      )}
      {...props}
    />
  );
}

type FormSelectProps = SelectHTMLAttributes<HTMLSelectElement>;

export function FormSelect({ className, ...props }: FormSelectProps) {
  return (
    <select
      className={cn(
        "min-h-11 w-full rounded-[10px] border border-input bg-surface px-3.5 py-[11px] text-ink",
        className,
      )}
      {...props}
    />
  );
}
