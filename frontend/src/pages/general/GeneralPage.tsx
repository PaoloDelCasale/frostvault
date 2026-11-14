import type { ReactNode } from "react";
import type { Overview } from "@/api/vault";
import { Panel } from "@/components/Panel";
import { UploadRetryActions } from "@/components/UploadRetryActions";
import { useI18n } from "@/i18n";
import { RecoveryKitSection } from "./RecoveryKitSection";
import { MetadataBackupsSection } from "./MetadataBackupsSection";
import { SourceVolumesSection } from "./SourceVolumesSection";

/** Same heading pattern as the kit, backup and source-volume sections. */
function SystemSection({ id, title, summary, children }: { id: string; title: string; summary: string; children: ReactNode }) {
  return <section aria-labelledby={`system-${id}-heading`} className="grid gap-4">
    <div>
      <h2 id={`system-${id}-heading`} className="text-xl font-bold">{title}</h2>
      <p className="mt-1 text-sm text-muted">{summary}</p>
    </div>
    <Panel className="grid gap-3 p-5">{children}</Panel>
  </section>;
}

/** Installation-wide settings only: per-Vault lists and actions live in Overview and Vault settings. */
export function GeneralPage({ overview }: { overview?: Overview }) {
  const { t } = useI18n();
  return <div className="grid min-w-0 gap-8">
    <div>
      <h1 className="text-2xl font-bold">{t("nav.system")}</h1>
      <p className="text-sm text-muted">{t("system.subtitle")}</p>
    </div>
    <RecoveryKitSection />
    <MetadataBackupsSection />
    <SourceVolumesSection />
    <SystemSection id="email" title={t("system.email_title")} summary={t("system.email_summary")}>
      {overview?.email_configured === false
        ? <p role="status" data-testid="email-not-configured">{t("system.email_off")}</p>
        : overview?.email_configured ? <p role="status">{t("system.email_on")}</p> : null}
      <details className="text-sm text-muted"><summary className="min-h-11 w-fit cursor-pointer py-2 font-semibold text-ink">{t("system.email_how")}</summary>
        <p>{t("essential.email_help")}</p></details>
    </SystemSection>
    <SystemSection id="uploads" title={t("system.uploads_title")} summary={t("system.uploads_summary")}>
      <UploadRetryActions />
    </SystemSection>
  </div>;
}
