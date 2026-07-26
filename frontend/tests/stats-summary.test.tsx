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
  },
  delete_enabled: false,
};

describe("StatsSummary from /api/stats", () => {
  it("renders statistic cards with correctly formatted counts and bytes", () => {
    render(<StatsSummary stats={realisticStats} t={t} />);

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
    render(<StatsSummary stats={realisticStats} t={t} />);

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
});
