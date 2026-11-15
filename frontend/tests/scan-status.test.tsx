import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ScanStatus } from "@/components/ScanStatus";

// Echo the key and its params, so the test reads what the component asked for.
const t = (key: string, params?: Record<string, string | number>) =>
  params ? `${key} ${JSON.stringify(params)}` : key;

describe("ScanStatus (#691)", () => {
  it("renders nothing for an idle, healthy Vault", () => {
    const { container } = render(<ScanStatus runtime={{ scanning: false, last_error_source: null }} t={t} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the verification progress of the running scan", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true, scan_phase: "verification",
      verify_files_total: 138822, verify_files_done: 1200,
      verify_bytes_total: 2 * 1024 ** 3, verify_bytes_done: 1024 ** 3,
    }} />);
    const line = screen.getByTestId("scan-in-progress").textContent ?? "";
    expect(line).toContain("vault.scan.in_progress");
    expect(line).toContain("vault.scan.verifying");
    expect(line).toContain("1.0 GiB");
    expect(line).toContain("2.0 GiB");
    expect(screen.queryByTestId("scan-last-failed")).toBeNull();
  });

  it("presents an older source error as the last scan's outcome while a new scan runs", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true,
      last_error_source: "Source scan blocked: Visited directory changed during traversal",
      last_error_source_at: "2026-11-15T12:58:00+00:00",
    }} />);
    const failed = screen.getByTestId("scan-last-failed").textContent ?? "";
    expect(failed).toContain("vault.scan.last_failed");
    expect(failed).toContain("Visited directory changed");
    expect(failed).not.toContain("2026-11-15T12:58");
    expect(screen.getByTestId("scan-in-progress").textContent).toContain("vault.scan.new_in_progress");
  });

  it("keeps the last failure with its time when no scan runs", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: false, last_error_source: "Source scan: boom", last_error_source_at: "2026-11-15T12:58:00+00:00",
    }} />);
    expect(screen.getByTestId("scan-last-failed")).toBeInTheDocument();
    expect(screen.queryByTestId("scan-in-progress")).toBeNull();
  });
});
