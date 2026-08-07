import { Card } from "@/components/Card";
import type { StatsResponse } from "@/api/types";
import { cn } from "@/lib/utils";

import { formatBytes, formatCount } from "./format";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export type StatsSummaryStatus = "loading" | "ready" | "error";

type StatsSummaryProps = {
  /** Authoritative stats payload; null/undefined until the first success. */
  stats: StatsResponse | null | undefined;
  /** Query lifecycle — never render placeholder zeros while loading. */
  status: StatsSummaryStatus;
  t: Translate;
  className?: string;
};

type StatCard = {
  labelKey: string;
  value: string;
};

const STAT_CARD_COUNT = 6;

function buildCards(stats: StatsResponse): StatCard[] {
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

function LoadingSkeleton({ t }: { t: Translate }) {
  const slots = Array.from({ length: STAT_CARD_COUNT }, (_, index) => index);
  return (
    <>
      <div
        data-testid="stats-compact"
        className="grid grid-cols-3 gap-2 md:hidden"
        aria-hidden="true"
      >
        {slots.map((slot) => (
          <div
            key={slot}
            data-testid="stats-skeleton-card"
            className="rounded-card border border-line bg-surface px-2 py-2"
          >
            <span className="mb-2 block h-3 w-16 animate-pulse rounded bg-line" />
            <span className="block h-5 w-10 animate-pulse rounded bg-line" />
          </div>
        ))}
      </div>
      <div
        data-testid="stats-expanded"
        className="hidden gap-3 md:grid md:grid-cols-3"
        aria-hidden="true"
      >
        {slots.map((slot) => (
          <Card
            key={slot}
            data-testid="stats-skeleton-card"
            className="p-3 md:p-4"
          >
            <span className="mb-3 block h-3.5 w-24 animate-pulse rounded bg-line" />
            <span className="block h-8 w-16 animate-pulse rounded bg-line" />
          </Card>
        ))}
      </div>
      <span className="sr-only">{t("ui.stats_loading")}</span>
    </>
  );
}

/**
 * Archive statistic cards.
 * Compact denser strip below `md`; expanded card grid from `md` up.
 * Loading never paints zeros; only an authoritative payload may show 0/0 B.
 */
export function StatsSummary({
  stats,
  status,
  t,
  className,
}: StatsSummaryProps) {
  const ready = status === "ready" && stats != null;
  const cards = ready ? buildCards(stats) : [];

  return (
    <section
      aria-label={t("ui.archive_statistics")}
      aria-busy={status === "loading" || undefined}
      data-testid="stats-summary"
      data-status={status}
      className={cn("mb-4", className)}
    >
      {status === "loading" ? <LoadingSkeleton t={t} /> : null}

      {status === "error" ? (
        <p
          role="alert"
          data-testid="stats-error"
          className="rounded-card border border-[var(--health-warn-border)] bg-red-soft px-4 py-3 text-sm text-ink"
        >
          {t("ui.stats_error")}
        </p>
      ) : null}

      {ready ? (
        <>
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
        </>
      ) : null}
    </section>
  );
}
