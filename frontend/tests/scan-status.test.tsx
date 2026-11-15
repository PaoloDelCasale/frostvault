import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ScanStatus } from "@/components/ScanStatus";
import { formatNumber } from "@/pages/archive/format";

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

  it("shows files seen and their rate during the traversal of a first adoption (#696)", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true, scan_phase: "traversal",
      traversal_files_seen: 96500, traversal_files_per_minute: 2144.4, traversal_files_expected: null,
    }} />);
    const line = screen.getByTestId("scan-traversal").textContent ?? "";
    expect(line).toContain("vault.scan.traversal ");
    expect(line).toContain(`"files":"${formatNumber(96500)}","rate":"${formatNumber(2144)}"`);
    expect(line).not.toContain("traversal_estimate");
    expect(line).not.toContain("remaining");
  });

  it("estimates the time left when a previous scan counted the Local Copies (#696)", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true, scan_phase: "traversal",
      traversal_files_seen: 96000, traversal_files_per_minute: 2000, traversal_files_expected: 138000,
    }} />);
    const line = screen.getByTestId("scan-traversal").textContent ?? "";
    expect(line).toContain("vault.scan.traversal_estimate");
    // 42,000 files left at 2,000 per minute: 21 minutes.
    expect(line).toContain('vault.scan.remaining_minutes {\\"count\\":21}');
  });

  it("states the time left in hours beyond an hour (#696)", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true, scan_phase: "traversal",
      traversal_files_seen: 1000, traversal_files_per_minute: 1000, traversal_files_expected: 91000,
    }} />);
    expect(screen.getByTestId("scan-traversal").textContent).toContain('vault.scan.remaining_hours {\\"count\\":1.5}');
  });

  it("drops the traversal line once the scan verifies (#696)", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true, scan_phase: "verification",
      traversal_files_seen: 138000, traversal_files_per_minute: 2000, traversal_files_expected: 138000,
      verify_files_total: 10, verify_files_done: 1, verify_bytes_total: 10, verify_bytes_done: 1,
    }} />);
    expect(screen.queryByTestId("scan-traversal")).toBeNull();
    expect(screen.getByTestId("scan-in-progress").textContent).toContain("vault.scan.verifying");
  });
});
