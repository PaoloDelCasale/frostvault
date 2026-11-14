import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SourceVolumesSection } from "@/pages/general/SourceVolumesSection";

const messages: Record<string, string> = {
  "general.sources_heading": "Source Volumes",
  "general.sources_subtitle": "Volumes",
  "general.sources_access": "Access",
  "general.sources_vault_count": "Vaults",
  "general.sources_health_absent": "Absent",
  "general.sources_health_inaccessible": "Inaccessible",
  "general.sources_health_replaced": "Replaced",
  "general.sources_diagnostic_absent": "The expected mount is absent.",
  "general.sources_diagnostic_inaccessible": "The mount is present but inaccessible.",
  "general.sources_diagnostic_replaced": "A different Source Volume is mounted.",
};

vi.mock("@/i18n/useI18n", () => ({
  useI18n: () => ({ t: (key: string) => messages[key] ?? key }),
}));

vi.mock("@/api", async () => {
  const actual = await vi.importActual<typeof import("@/api")>("@/api");
  return {
    ...actual,
    fetchGeneralSourceVolumes: vi.fn(async () => ({
      items: [
        { alias: "absent", path: "/sources/absent", access: "none", health: "absent", vault_count: 1, diagnostic: null },
        { alias: "locked", path: "/sources/locked", access: "ro", health: "inaccessible", vault_count: 0, diagnostic: null },
        { alias: "replaced", path: "/sources/replaced", access: "rw", health: "replaced", vault_count: 1, diagnostic: null },
      ],
    })),
  };
});

afterEach(cleanup);

describe("Source Volume identity diagnostics", () => {
  it("distinguishes absent, inaccessible, and replaced volumes", async () => {
    render(<SourceVolumesSection />);

    expect(await screen.findByText("Absent")).toBeInTheDocument();
    expect(screen.getByText("Inaccessible")).toBeInTheDocument();
    expect(screen.getByText("Replaced")).toBeInTheDocument();
    expect(screen.getByText("The expected mount is absent.")).toBeInTheDocument();
    expect(screen.getByText("The mount is present but inaccessible.")).toBeInTheDocument();
    expect(screen.getByText("A different Source Volume is mounted.")).toBeInTheDocument();
  });
});

describe("Source Volume health tone (#658)", () => {
  it("colours unusable volumes as a problem, not neutral", async () => {
    render(<SourceVolumesSection />);
    const badges = await screen.findAllByTestId("source-health");
    expect(badges.map((badge) => badge.getAttribute("data-tone"))).toEqual(["bad", "bad", "bad"]);
    expect(badges[1]).toHaveClass("bg-[var(--state-problem-bg)]");
  });
});
