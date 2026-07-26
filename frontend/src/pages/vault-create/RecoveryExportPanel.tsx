import type { ReactNode } from "react";

type RecoveryExportPanelProps = {
  recoveryExport: string;
  title: string;
  subtitle: string;
  exportLabel: string;
  warning: string;
  children?: ReactNode;
};

/** Read-only recovery material — no textarea; mobile-legible pre block. */
export function RecoveryExportPanel({
  recoveryExport,
  title,
  subtitle,
  exportLabel,
  warning,
  children,
}: RecoveryExportPanelProps) {
  return (
    <div className="mt-6 grid gap-3.5">
      <h2 className="m-0 text-xl font-bold text-ink">{title}</h2>
      <p className="m-0 text-sm text-muted">{subtitle}</p>
      <p
        className="m-0 rounded-[10px] bg-amber-soft px-3.5 py-3 text-sm text-ink"
        role="status"
        data-testid="recovery-custody-warning"
      >
        {warning}
      </p>
      <div className="grid gap-1.5">
        <span className="text-[13px] font-bold text-muted">{exportLabel}</span>
        <pre
          data-testid="recovery-export-material"
          className="max-h-[min(50vh,22rem)] overflow-auto whitespace-pre-wrap break-all rounded-[10px] border border-line bg-canvas p-3.5 font-mono text-[12px] leading-relaxed text-ink"
          tabIndex={0}
        >
          {recoveryExport}
        </pre>
      </div>
      {children}
    </div>
  );
}
