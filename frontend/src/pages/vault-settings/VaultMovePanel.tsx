import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { apiQueryKeys, readOnMount, vaultApi, type VaultMovePreview } from "@/api";
import { MenuSelect } from "@/components/MenuSelect";
import { ActiveOperations } from "@/components/ActiveOperations";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { formatBytes, formatDate, formatEur } from "@/pages/archive/format";
import { errorMessage } from "@/lib/errorMessage";

/** "≈ 0.27 €"; a sub-cent "< 0.01 €" is already a bound, so it gets no "≈". */
const approximately = (amount: string) => amount.startsWith("<") ? amount : `≈ ${amount}`;

export function VaultMovePanel({ vaultId, onNotice }: { vaultId: number; onNotice: (message: string, error?: boolean) => void }) {
  const { t, locale } = useI18n();
  const [preview, setPreview] = useState<VaultMovePreview | null>(null);
  const [target, setTarget] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // A failed preview (#718) is reported inline; the panel stays usable.
  const [previewError, setPreviewError] = useState("");
  const translateRef = useRef(t);
  translateRef.current = t;
  const loaded = useQuery({ queryKey: apiQueryKeys.vaultMove(vaultId), queryFn: () => vaultApi(vaultId).move(), ...readOnMount });
  useEffect(() => {
    const next = loaded.data;
    if (!next) return;
    if (next.vault_id !== vaultId) {
      setError(translateRef.current("move.vault_changed"));
      return;
    }
    // An answer that is not a preview is reported like a read that failed.
    try {
      setPreviewError("");
      setPreview(next);
      setTarget(next.targets.find((item) => item.eligible_count > 0)?.target_class ?? "");
    } catch (cause) { setPreviewError(errorMessage(cause, translateRef.current)); }
  }, [loaded.data, vaultId]);
  useEffect(() => { if (loaded.error) setPreviewError(errorMessage(loaded.error, translateRef.current)); }, [loaded.error]);
  const estimate = preview?.targets.find((item) => item.target_class === target);
  async function move() {
    if (!target || !acknowledged) return;
    setBusy(true);
    setError("");
    try {
      const result = await vaultApi(vaultId).startMove({ target_class: target, acknowledge_costs: true });
      onNotice(t("move.queued", { count: result.item_count }));
      setAcknowledged(false);
    } catch (cause) {
      setError(errorMessage(cause, t));
      setBusy(false);
      return;
    }
    try {
      const next = await vaultApi(vaultId).move();
      if (next.vault_id !== vaultId) {
        setPreview(null);
        setError(t("move.vault_changed"));
      } else {
        setPreviewError("");
        setPreview(next);
      }
    } catch (cause) {
      // The Jobs are queued: only the refreshed preview is unavailable.
      setPreview(null);
      setPreviewError(errorMessage(cause, t));
    } finally { setBusy(false); }
  }
  return <section className="mt-5 grid gap-3 border-t border-line pt-4" data-panel="vault-move">
    <h3 className="font-bold">{t("move.title")}</h3>
    <p className="text-sm text-muted">{t("move.help")}</p>
    {preview && preview.targets.some((item) => item.eligible_count > 0) ? <>
      <MenuSelect label={t("move.target")} value={target} disabled={busy}
        onValueChange={(value) => { setTarget(value); setAcknowledged(false); }}
        options={preview.targets.filter((item) => item.eligible_count > 0).map((item) => ({ value: item.target_class, label: t(`storage.${item.target_class}`) }))} />
      {estimate && <>
        <div className="grid gap-1" data-testid="vault-move-estimate">
          <p className="text-sm"><strong className="text-base">{t("move.estimate_monthly", { monthly: approximately(formatEur(estimate.estimated_monthly_eur, locale)) })}</strong>{" "}
            {t("move.estimate_scope", { count: estimate.eligible_count, size: formatBytes(estimate.total_bytes, locale) })}</p>
          <p className="text-sm text-muted">{t("move.estimate_pricing", { retrieval: formatEur(estimate.estimated_retrieval_eur, locale), region: estimate.region, date: formatDate(estimate.pricing_effective_at, locale) })}</p>
        </div>
        <p className="text-sm text-muted">{t("move.exclusions")}</p>
        <p className="text-sm">{t("move.minimum", { days: estimate.minimum_days })}</p>
        {Object.entries(estimate.skipped).map(([reason, count]) => <p className="text-sm text-muted" key={reason}>{t(`move.skip_${reason}`, { count })}</p>)}
      </>}
      <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={acknowledged} disabled={busy} onChange={(event) => setAcknowledged(event.target.checked)} />{t("move.acknowledge")}</label>
      <Button type="button" variant="primary" disabled={busy || !acknowledged || !estimate?.eligible_count} className="w-fit" onClick={() => void move()}>{t("move.start")}</Button>
    </> : preview ? <p className="text-sm text-muted">{t("move.unavailable")}</p>
      : previewError ? <p role="status" className="text-sm text-muted" data-testid="vault-move-preview-unavailable">{t("move.preview_unavailable", { detail: previewError })}</p>
      : !error && <p role="status">{t("ui.loading")}</p>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    <ActiveOperations vaultId={vaultId} t={t} moves />
  </section>;
}
