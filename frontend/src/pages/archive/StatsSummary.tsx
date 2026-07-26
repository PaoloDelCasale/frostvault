import { Card } from "@/components/Card";
import type { StatsResponse } from "@/api/types";
import { cn } from "@/lib/utils";

import { formatBytes, formatCount } from "./format";

type Translate = (key: string, params?: Record<string, string | number>) => string;

type StatsSummaryProps = {
  stats: StatsResponse;
  t: Translate;
  className?: string;
};

type StatCard = {
  labelKey: string;
  value: string;
};

function buildCards(stats: StatsResponse, t: Translate): StatCard[] {
  const states = stats.states || {};
  const storage = stats.storage || { local_bytes: 0, cloud_bytes: 0 };
  return [
    { labelKey: "state.both", value: formatCount(states.both || 0) },
    { labelKey: "state.local_only", value: formatCount(states.local_only || 0) },
    { labelKey: "state.cloud_only", value: formatCount(states.cloud_only || 0) },
    { labelKey: "ui.server_space", value: formatBytes(storage.local_bytes || 0) },
    { labelKey: "ui.cloud_space", value: formatBytes(storage.cloud_bytes || 0) },
    {
      labelKey: "ui.active_operations",
      value: formatCount(stats.active_jobs || 0),
    },
  ];
}

function StatCardView({ label, value }: { label: string; value: string }) {
  return (
    <Card className="p-3 md:p-4">
      <span className="text-[13px] text-muted">{label}</span>
      <strong className="mt-1 block text-xl tracking-tight md:text-[27px]">
        {value}
      </strong>
    </Card>
  );
}

/**
 * Archive statistic cards.
 * Compact denser strip below `md`; expanded card grid from `md` up.
 */
export function StatsSummary({ stats, t, className }: StatsSummaryProps) {
  const cards = buildCards(stats, t);

  return (
    <section
      aria-label={t("ui.archive_statistics")}
      className={cn("mb-4", className)}
    >
      {/* Compact form — below md */}
      <div
        data-testid="stats-compact"
        className="grid grid-cols-3 gap-2 md:hidden"
      >
        {cards.map((card) => (
          <div
            key={card.labelKey}
            className="rounded-card border border-line bg-surface px-2 py-2"
          >
            <span className="block truncate text-[11px] leading-tight text-muted">
              {t(card.labelKey)}
            </span>
            <strong className="mt-0.5 block text-sm font-bold tracking-tight">
              {card.value}
            </strong>
          </div>
        ))}
      </div>

      {/* Expanded form — md and up */}
      <div
        data-testid="stats-expanded"
        className="hidden gap-3 md:grid md:grid-cols-3"
      >
        {cards.map((card) => (
          <StatCardView
            key={card.labelKey}
            label={t(card.labelKey)}
            value={card.value}
          />
        ))}
      </div>
    </section>
  );
}
