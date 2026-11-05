import type { ArchiveListItem } from "@/api/types";
import type { BadgeState } from "@/components/Badge";
export function itemStateBadge(item: ArchiveListItem, t: (key: string, params?: Record<string, string | number>) => string) {
  let label = t(`state.${item.state}`);
  if (item.state === "unconfirmed" && typeof item.last_known_state === "string") label += ` · ${t("state.last_known", { state: t(`state.${item.last_known_state}`), date: String(item.last_known_at ?? "—") })}`;
  if (item.previous_snapshot_protected) label += ` · ${t("state.previous_snapshot_protected")}`;
  return { state: item.state as BadgeState, label };
}
