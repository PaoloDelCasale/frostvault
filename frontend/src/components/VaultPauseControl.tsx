import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Pause, Play } from "lucide-react";
import { apiRequest } from "@/api";
import type { components } from "@/api/openapi.generated";
import { useI18n } from "@/i18n";
import { Button } from "@/components/ui/button";
import { notify } from "@/components/toastQueue";
import { errorMessage } from "@/lib/errorMessage";

/** A hold is independent of protection/source state; never replaces their badge. */
export function VaultPauseControl({ vaultId, paused }: { vaultId: number; paused: boolean | undefined }) {
  const { t } = useI18n();
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  async function toggle() {
    if (paused === undefined || busy) return;
    setBusy(true);
    try {
      const result = await apiRequest<components["schemas"]["VaultPauseResponse"]>(
        `/api/vaults/${vaultId}/${paused ? "resume" : "pause"}`, { method: "POST" },
      );
      notify(result.message);
      await client.invalidateQueries({ queryKey: ["general-vaults"] });
      await client.invalidateQueries({ queryKey: ["vaults"] });
      await client.invalidateQueries({ queryKey: ["notifications"] });
    } catch (error) { notify(errorMessage(error, t), "error"); }
    finally { setBusy(false); }
  }
  return <div className="grid gap-2" data-testid={`vault-pause-${vaultId}`}>
    {paused && <span role="status" className="w-fit rounded-full border border-line px-2 py-1 text-xs font-bold" title={t("vault_pause.help")}>{t("vault_pause.paused")}</span>}
    <Button variant="secondary" size="sm" className="w-fit" disabled={busy || paused === undefined} onClick={() => void toggle()}>
      {paused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}{t(paused ? "vault_pause.resume" : "vault_pause.pause")}
    </Button>
  </div>;
}
