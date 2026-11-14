import { useEffect, useRef, useState } from "react";
import { fetchOperationPolicy, updateOperationPolicy, type OperationPolicy } from "@/api";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";

type VerificationChoice = "inherit" | "s3_checksum" | "read_back";
const choiceOf = (policy: OperationPolicy | null): VerificationChoice => policy?.upload_verification ?? "inherit";

/** A field, not a switch: the choice is saved with «Salva», enabled only for a change (#658). */
export function UploadVerificationPanel({ vaultId, onNotice }: { vaultId: number; onNotice: (message: string, error?: boolean) => void }) {
  const { t } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [policy, setPolicy] = useState<OperationPolicy | null>(null);
  const [choice, setChoice] = useState<VerificationChoice>("inherit");
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  useEffect(() => {
    let live = true;
    setPolicy(null); setLoadError("");
    void fetchOperationPolicy(vaultId).then((data) => { if (live) { setPolicy(data); setChoice(choiceOf(data)); } })
      .catch((error: unknown) => { if (live) setLoadError(errorMessage(error, translateRef.current)); });
    return () => { live = false; };
  }, [vaultId]);
  const dirty = policy !== null && choice !== choiceOf(policy);
  async function save() {
    if (!dirty) return;
    setBusy(true);
    try {
      const next = await updateOperationPolicy({ upload_verification: choice === "inherit" ? null : choice }, vaultId);
      setPolicy(next); setChoice(choiceOf(next));
      onNotice(t("settings.verification_saved"));
    } catch (error) { onNotice(errorMessage(error, t), true); }
    finally { setBusy(false); }
  }
  return <section className="grid gap-2">
    {loadError && <p role="alert" className="text-sm text-destructive">{loadError}</p>}
    <label className="grid min-w-0 gap-2 font-semibold"><span className="sr-only">{t("verification.title")}</span>
      <select className="min-h-11 w-full min-w-0 truncate rounded border border-line bg-surface px-3" disabled={!policy || busy} value={choice}
        onChange={(event) => setChoice(event.target.value === "read_back" ? "read_back" : event.target.value === "s3_checksum" ? "s3_checksum" : "inherit")}>
        <option value="inherit">{t("verification.inherit")}</option>
        <option value="s3_checksum">{t("verification.s3_checksum")}</option>
        <option value="read_back">{t("verification.read_back")}</option>
      </select>
    </label>
    <p className="text-sm text-muted">{t("verification.global")}: {policy?.global_upload_verification ? t(`verification.${policy.global_upload_verification}`) : "—"}</p>
    <p className="text-sm text-muted">{t("verification.effective")}: {policy?.effective_upload_verification ? t(`verification.${policy.effective_upload_verification}`) : "—"}</p>
    <Button type="button" variant={dirty ? "primary" : "secondary"} className="min-h-11 w-full sm:w-fit sm:justify-self-start" disabled={busy || !dirty} onClick={() => void save()}>
      {t("settings.verification_save")}
    </Button>
  </section>;
}
