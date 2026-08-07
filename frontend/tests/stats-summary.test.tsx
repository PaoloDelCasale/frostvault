import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { StatsResponse } from "@/api/types";
import { StatsSummary } from "@/pages/archive/StatsSummary";

const messages: Record<string, string> = {
  "state.both": "Server + cloud",
  "state.local_only": "Server only",
  "state.cloud_only": "Cloud only",
  "ui.server_space": "Server space",
  "ui.cloud_space": "Cloud space",
  "ui.active_operations": "Active operations",
  "ui.archive_statistics": "Archive statistics",
  "ui.stats_loading": "Loading archive statistics…",
  "ui.stats_error":
    "Unable to load archive statistics. Values will appear when the request succeeds.",
};

function t(key: string): string {
  return messages[key] ?? key;
}

/** Realistic /api/stats payload (counts + mixed byte scales). */
const realisticStats: StatsResponse = {
  states: { both: 12, local_only: 3, cloud_only: 1042 },
  storage: { local_bytes: 1536, cloud_bytes: 1099511627776 },
  active_jobs: 2,
  runtime: {},
  filesystem: {
    ok: true,
    uid: 1000,
    gid: 1000,
    root: "/sources/test",
    checks: [],
    findings: [],
    health_status: "current",
    findings_total: 0,
    finding_counts: {},
    findings_truncated: false,
  },
  delete_enabled: false,
};

const emptyVaultStats: StatsResponse = {
  states: { both: 0, local_only: 0, cloud_only: 0 },
  storage: { local_bytes: 0, cloud_bytes: 0 },
  active_jobs: 0,
  runtime: {},
  filesystem: {
    ok: true,
    uid: 1000,
    gid: 1000,
    root: "/sources/empty",
    checks: [],
    findings: [],
    health_status: "current",
    findings_total: 0,
  },
  delete_enabled: false,
};

describe("StatsSummary from /api/stats", () => {
  it("renders statistic cards with correctly formatted counts and bytes", () => {
    render(<StatsSummary stats={realisticStats} status="ready" t={t} />);

    // Both compact and expanded trees are in the DOM (CSS toggles visibility),
    // so assert via getAllByText that the formatted values are present.
    expect(screen.getAllByText("Server + cloud").length).toBeGreaterThan(0);
    expect(screen.getAllByText("12").length).toBeGreaterThan(0);

    expect(screen.getAllByText("Server only").length).toBeGreaterThan(0);
    expect(screen.getAllByText("3").length).toBeGreaterThan(0);

    expect(screen.getAllByText("Cloud only").length).toBeGreaterThan(0);
    expect(screen.getAllByText("1,042").length).toBeGreaterThan(0);

    expect(screen.getAllByText("Server space").length).toBeGreaterThan(0);
    expect(screen.getAllByText("1.5 KB").length).toBeGreaterThan(0);

    expect(screen.getAllByText("Cloud space").length).toBeGreaterThan(0);
    expect(screen.getAllByText("1.0 TB").length).toBeGreaterThan(0);

    expect(screen.getAllByText("Active operations").length).toBeGreaterThan(0);
    expect(screen.getAllByText("2").length).toBeGreaterThan(0);
  });

  it("activates the compact form below md and the expanded form from md up", () => {
    render(<StatsSummary stats={realisticStats} status="ready" t={t} />);

    const compact = screen.getByTestId("stats-compact");
    const expanded = screen.getByTestId("stats-expanded");

    // Tailwind responsive utilities: compact visible by default, hidden at md+;
    // expanded hidden by default, grid from md+.
    expect(compact.className.split(/\s+/)).toEqual(
      expect.arrayContaining(["md:hidden"]),
    );
    expect(expanded.className.split(/\s+/)).toEqual(
      expect.arrayContaining(["hidden", "md:grid"]),
    );
  });

  it("shows a loading skeleton without painting placeholder zeros", () => {
    render(<StatsSummary stats={null} status="loading" t={t} />);

    const summary = screen.getByTestId("stats-summary");
    expect(summary).toHaveAttribute("data-status", "loading");
    expect(summary).toHaveAttribute("aria-busy", "true");
    // Compact + expanded skeletons both mount; Card may not forward test ids.
    expect(screen.getAllByTestId("stats-skeleton-card").length).toBeGreaterThanOrEqual(6);
    expect(screen.getByText("Loading archive statistics…")).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    expect(screen.queryByText("0 B")).not.toBeInTheDocument();
  });

  it("shows a distinct error state without fake zeros", () => {
    render(<StatsSummary stats={null} status="error" t={t} />);

    expect(screen.getByTestId("stats-error")).toHaveTextContent(
      /Unable to load archive statistics/i,
    );
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    expect(screen.queryByText("0 B")).not.toBeInTheDocument();
  });

  it("may render authoritative zeros only after a successful empty-vault payload", () => {
    render(<StatsSummary stats={emptyVaultStats} status="ready" t={t} />);

    expect(screen.getAllByText("0").length).toBeGreaterThanOrEqual(6);
    expect(screen.getAllByText("0 B").length).toBeGreaterThan(0);
  });
});
