export function VerificationEvidence({ method, requested, fallback, egress, t }: {
  method?: string | null; requested?: string | null; fallback?: string | null; egress?: number | null;
  t: (key: string, params?: Record<string, string | number>) => string;
}) {
  return <section className="grid gap-2 text-sm">
    <p>{t("verification.requested")}: {requested ? t(`verification.${requested}`) : "—"}</p>
    <p>{t("verification.method")}: {method ? t(`verification.${method}`) : "—"}</p>
    {fallback && <p role="status">{t(`verification.${fallback}`)}</p>}
    {method === "read_back" && <p>{t("verification.egress")}{egress != null ? ` (${egress.toLocaleString()} bytes)` : ""}</p>}
  </section>;
}
