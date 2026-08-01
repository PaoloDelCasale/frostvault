import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SourceVolumesSection } from "@/pages/admin/SourceVolumesSection";

const messages: Record<string, string> = {
  "admin.sources_heading": "Source Volumes",
  "admin.sources_subtitle": "Volumes",
  "admin.sources_access": "Access",
  "admin.sources_vault_count": "Vaults",
  "admin.sources_source_area_count": "Source Areas",
  "admin.sources_health_absent": "Absent",
  "admin.sources_health_inaccessible": "Inaccessible",
  "admin.sources_health_replaced": "Replaced",
  "admin.sources_diagnostic_absent": "The expected mount is absent.",
  "admin.sources_diagnostic_inaccessible": "The mount is present but inaccessible.",
  "admin.sources_diagnostic_replaced": "A different Source Volume is mounted.",
  "admin.source_areas_heading": "Source Areas",
  "admin.source_areas_empty": "No areas",
  "admin.source_areas_assign_open": "Assign",
};

vi.mock("@/i18n/useI18n", () => ({
  useI18n: () => ({ t: (key: string) => messages[key] ?? key }),
}));

vi.mock("@/api", async () => {
  const actual = await vi.importActual<typeof import("@/api")>("@/api");
  return {
    ...actual,
    fetchAdminSourceVolumes: vi.fn(async () => ({
      items: [
        { alias: "absent", path: "/sources/absent", access: "none", health: "absent", vault_count: 1, source_area_count: 0, diagnostic: null },
        { alias: "locked", path: "/sources/locked", access: "ro", health: "inaccessible", vault_count: 0, source_area_count: 0, diagnostic: null },
        { alias: "replaced", path: "/sources/replaced", access: "rw", health: "replaced", vault_count: 1, source_area_count: 1, diagnostic: null },
      ],
    })),
    fetchAdminSourceAreas: vi.fn(async () => ({ items: [] })),
    fetchAdminUsers: vi.fn(async () => ({ items: [] })),
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
