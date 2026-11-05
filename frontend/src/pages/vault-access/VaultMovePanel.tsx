import { useEffect, useState } from "react";

import { fetchVaultMove, startVaultMove, type VaultMovePreview } from "@/api";
import { MenuSelect } from "@/components/MenuSelect";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

export function VaultMovePanel({ onNotice }: { onNotice: (message: string, error?: boolean) => void }) {
  const { t } = useI18n();
  const [preview, setPreview] = useState<VaultMovePreview | null>(null);
  const [target, setTarget] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    void fetchVaultMove().then((next) => {
      if (live) {
        setPreview(next);
        setTarget(next.targets.find((item) => item.eligible_count > 0)?.target_class ?? "");
      }
    }).catch((cause: unknown) => { if (live) setError(String(cause)); });
    return () => { live = false; };
  }, []);
  const estimate = preview?.targets.find((item) => item.target_class === target);
  async function move() {
    if (!target || !acknowledged) return;
    setBusy(true);
    setError("");
    try {
      const result = await startVaultMove({ target_class: target, acknowledge_costs: true });
      onNotice(t("move.queued", { count: result.item_count }));
      setAcknowledged(false);
      setPreview(await fetchVaultMove());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
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
        <p className="text-sm">{t("move.estimate", { count: estimate.eligible_count, bytes: estimate.total_bytes, monthly: estimate.estimated_monthly_eur, retrieval: estimate.estimated_retrieval_eur, region: estimate.region, date: estimate.pricing_effective_at })}</p>
        <p className="text-sm text-muted">{t("move.exclusions")}</p>
        <p className="text-sm">{t("move.minimum", { days: estimate.minimum_days })}</p>
        {Object.entries(estimate.skipped).map(([reason, count]) => <p className="text-sm text-muted" key={reason}>{t(`move.skip_${reason}`, { count })}</p>)}
      </>}
      <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={acknowledged} disabled={busy} onChange={(event) => setAcknowledged(event.target.checked)} />{t("move.acknowledge")}</label>
      <Button type="button" variant="primary" disabled={busy || !acknowledged || !estimate?.eligible_count} className="w-fit" onClick={() => void move()}>{t("move.start")}</Button>
    </> : preview ? <p className="text-sm text-muted">{t("move.unavailable")}</p> : !error && <p role="status">{t("ui.loading")}</p>}
    {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
  </section>;
}
