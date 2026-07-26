import type { FilesystemHealth } from "@/api/types";
import { cn } from "@/lib/utils";

type Translate = (key: string, params?: Record<string, string | number>) => string;

type FilesystemHealthBannerProps = {
  filesystem: FilesystemHealth | null | undefined;
  t: Translate;
  className?: string;
};

/**
 * Alarm banner for vault filesystem health.
 * When `ok` is true there is no alarm (operators still see stats elsewhere).
 * When not ok, every finding is listed with its remediation text.
 */
export function FilesystemHealthBanner({
  filesystem,
  t,
  className,
}: FilesystemHealthBannerProps) {
  if (!filesystem || filesystem.ok) {
    return null;
  }

  const findings = filesystem.findings || [];
  const checkRemediations = (filesystem.checks || [])
    .map((check) => check.remediation)
    .filter((text): text is string => Boolean(text));

  return (
    <section
      role="alert"
      data-testid="filesystem-health"
      className={cn(
        "filesystem-health warn mb-4 rounded-card border border-[#efd48a] bg-amber-soft px-4 py-3.5",
        className,
      )}
    >
      <strong className="mb-1 block">
        {t("ui.filesystem_needs_attention")}
      </strong>
      <span className="text-[13px] text-muted">
        {t("ui.filesystem_attention_detail")}
        {filesystem.uid != null && filesystem.gid != null
          ? ` uid=${filesystem.uid} gid=${filesystem.gid}.`
          : null}
      </span>
      {findings.length > 0 ? (
        <ul className="mt-2.5 list-disc space-y-1 pl-4 text-[13px]">
          {findings.map((finding) => {
            const remediation =
              finding.remediation ||
              checkRemediations[0] ||
              null;
            return (
              <li key={`${finding.path}:${finding.code}`}>
                <code className="text-[12px]">{finding.path || ""}</code>
                {" — "}
                {finding.message || finding.code}
                {remediation ? (
                  <>
                    {" — "}
                    <span className="text-muted">{remediation}</span>
                  </>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      {findings.length === 0 && checkRemediations.length > 0 ? (
        <ul className="mt-2.5 list-disc space-y-1 pl-4 text-[13px]">
          {checkRemediations.map((text) => (
            <li key={text}>{text}</li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
