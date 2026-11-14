import { Badge, type BadgeState } from "@/components/Badge";

/** `manual` adds a neutral «Upload manuale» note; it never changes the tone (#647). */
export function VaultStateBadge({ state, t, size = "sm", manual = false }: {
  state: string; t: (key: string) => string; size?: "sm" | "default"; manual?: boolean;
}) {
  if (state === "unknown") return <span className="text-muted">—</span>;
  const badge = state === "empty"
    ? <Badge state="excluded" size={size} label={t("overview.empty_vault")} />
    : <Badge state={state as BadgeState} size={size} label={t(`state.${state}`)} />;
  if (!manual) return badge;
  return <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">{badge}
    <span className="text-xs text-muted" data-testid="manual-upload-note">{t("state.manual_upload")}</span></span>;
}
