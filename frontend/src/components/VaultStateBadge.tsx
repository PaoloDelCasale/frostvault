import { Badge, type BadgeState } from "@/components/Badge";

export function VaultStateBadge({ state, t, size = "sm" }: { state: string; t: (key: string) => string; size?: "sm" | "default" }) {
  if (state === "unknown") return <span className="text-muted">—</span>;
  if (state === "suspended") return <Badge state="problem" size={size} label={t("state.protection_suspended")} />;
  if (state === "empty") return <Badge state="excluded" size={size} label={t("overview.empty_vault")} />;
  return <Badge state={state as BadgeState} size={size} label={t(`state.${state}`)} />;
}
