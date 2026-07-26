type Translate = (key: string, params?: Record<string, string | number>) => string;

type SafetyFooterProps = {
  displayName: string;
  t: Translate;
};

/** Safety note at the bottom of the archive page (`.safety`). */
export function SafetyFooter({ displayName, t }: SafetyFooterProps) {
  return (
    <section
      data-testid="safety-footer"
      className="mt-4 flex items-start gap-3 rounded-card border border-line bg-surface px-4 py-3.5"
    >
      <span aria-hidden="true" className="text-lg leading-none">
        🔒
      </span>
      <div className="text-sm">
        <strong className="block">
          {t("ui.protected_archive", { name: displayName })}
        </strong>
        <span className="text-muted">{t("ui.protected_archive_detail")}</span>
      </div>
    </section>
  );
}
