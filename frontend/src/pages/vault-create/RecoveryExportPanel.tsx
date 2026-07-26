import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";

type RecoveryExportPanelProps = {
  recoveryExport: string;
  title: string;
  subtitle: string;
  exportLabel: string;
  warning: string;
  copyLabel: string;
  downloadLabel: string;
  downloadFilename?: string;
  showWarning?: boolean;
  children?: ReactNode;
  onCopy?: (material: string) => void | Promise<void>;
  onDownload?: (material: string) => void;
};

async function copyToClipboard(material: string): Promise<void> {
  await navigator.clipboard.writeText(material);
}

function downloadTextFile(material: string, filename: string): void {
  const blob = new Blob([material], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/** Read-only recovery material — no textarea; mobile-legible pre block. */
export function RecoveryExportPanel({
  recoveryExport,
  title,
  subtitle,
  exportLabel,
  warning,
  copyLabel,
  downloadLabel,
  downloadFilename = "frostvault-recovery-export.conf",
  showWarning = true,
  children,
  onCopy,
  onDownload,
}: RecoveryExportPanelProps) {
  return (
    <div className="mt-6 grid gap-3.5">
      <h2 className="m-0 text-xl font-bold text-ink">{title}</h2>
      <p className="m-0 text-sm text-muted">{subtitle}</p>
      {showWarning ? (
        <p
          className="m-0 rounded-[10px] bg-amber-soft px-3.5 py-3 text-sm text-ink"
          role="status"
          data-testid="recovery-custody-warning"
        >
          {warning}
        </p>
      ) : null}
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
      <div className="flex flex-wrap gap-2.5">
        <Button
          type="button"
          variant="secondary"
          onClick={() => {
            void (onCopy ?? copyToClipboard)(recoveryExport);
          }}
        >
          {copyLabel}
        </Button>
        <Button
          type="button"
          variant="secondary"
          onClick={() => {
            (onDownload ?? ((material) => downloadTextFile(material, downloadFilename)))(
              recoveryExport,
            );
          }}
        >
          {downloadLabel}
        </Button>
      </div>
      {children}
    </div>
  );
}
