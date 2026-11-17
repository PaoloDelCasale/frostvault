import type { UploadThroughput } from "@/api/types";
import { formatMegabytesPerSecond, type Phase } from "@/lib/throughput";

type Translate = (key: string, params?: Record<string, string | number>) => string;
const WIDTH = 120;
const HEIGHT = 24;
const FIELDS: Record<Phase, "upload_bytes_per_second" | "verification_bytes_per_second"> = {
  upload: "upload_bytes_per_second", verification: "verification_bytes_per_second",
};

/**
 * A phase's speed over the last 15 minutes, from the server's in-memory
 * samples (#755). Decorative next to the measured speeds it summarises: its
 * label states the range in words.
 */
export function SpeedSparkline({ throughput, phase, t }: { throughput: UploadThroughput | undefined; phase: Phase; t: Translate }) {
  const points = throughput?.history ?? [];
  if (points.length < 2) return null;
  const values = points.map((point) => Number(point[FIELDS[phase]]) || 0);
  const peak = Math.max(...values);
  if (peak <= 0) return null;
  const step = WIDTH / (values.length - 1);
  const path = values.map((value, index) => `${(index * step).toFixed(1)},${(HEIGHT - 1 - (value / peak) * (HEIGHT - 2)).toFixed(1)}`).join(" ");
  const label = t("estimate.history_label", {
    minutes: Math.round((throughput?.history_seconds ?? 900) / 60), peak: formatMegabytesPerSecond(peak, t),
  });
  return <svg role="img" aria-label={label} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} width={WIDTH} height={HEIGHT}
    className="shrink-0 text-green" data-testid={`sparkline-${phase}`}>
    <title>{label}</title>
    <polyline points={path} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
  </svg>;
}
