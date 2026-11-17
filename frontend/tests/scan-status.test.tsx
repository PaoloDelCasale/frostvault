import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ScanStatus } from "@/components/ScanStatus";
import { formatNumber, formatTimeOfDay } from "@/pages/archive/format";

// Echo the key and its params, so the test reads what the component asked for.
const t = (key: string, params?: Record<string, string | number>) =>
  params ? `${key} ${JSON.stringify(params)}` : key;
const phase = (name: string) => screen.getByTestId(`scan-phase-${name}`);

describe("ScanStatus (#691, #711)", () => {
  it("renders nothing for an idle, healthy Vault", () => {
    const { container } = render(<ScanStatus runtime={{ scanning: false, last_error_source: null }} t={t} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("says what a queued scan waits for and since when (#759)", () => {
    const since = "2026-11-17T17:30:00Z";
    render(<ScanStatus t={t} runtime={{
      scanning: false, scan_queued: true, scan_queued_at: "2026-11-17T17:45:00Z",
      scan_waiting_for: "identify_local_copies", scan_waiting_since: since,
    }} />);
    expect(screen.getByTestId("scan-queued")).toHaveTextContent(`vault.scan.queued ${JSON.stringify({
      operation: "vault.operation.identify_local_copies", time: formatTimeOfDay(new Date(since)) })}`);
    expect(screen.queryByTestId("scan-in-progress")).toBeNull();
  });

  it("names an unknown operation generically, and a free Vault as a scan about to start (#759)", () => {
    const { unmount } = render(<ScanStatus t={t} runtime={{
      scanning: false, scan_queued: true, scan_waiting_for: "mystery", scan_waiting_since: "2026-11-17T17:30:00Z",
    }} />);
    expect(screen.getByTestId("scan-queued")).toHaveTextContent("vault.operation.other");
    unmount();
    render(<ScanStatus t={t} runtime={{ scanning: false, scan_queued: true, scan_waiting_for: null, scan_waiting_since: null }} />);
    expect(screen.getByTestId("scan-queued")).toHaveTextContent("vault.scan.queued_starting");
  });

  it("lists the phases in order with their state (#711)", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true, scan_phase: "verification",
      verify_files_total: 22425, verify_files_done: 4495,
      verify_bytes_total: 80 * 1024 ** 3, verify_bytes_done: 20 * 1024 ** 3,
    }} />);
    const phases = within(screen.getByTestId("scan-in-progress")).getAllByRole("listitem");
    expect(phases.map((item) => item.dataset.testid)).toEqual(
      ["scan-phase-traversal", "scan-phase-verification", "scan-phase-finalization"]);
    expect(phase("traversal").dataset.state).toBe("done");
    expect(phase("verification").dataset.state).toBe("running");
    expect(phase("finalization").dataset.state).toBe("waiting");
  });

  it("shows the verification progress by bytes, with a bar", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true, scan_phase: "verification",
      verify_files_total: 138822, verify_files_done: 1200,
      verify_bytes_total: 2 * 1024 ** 3, verify_bytes_done: 1024 ** 3,
    }} />);
    const line = screen.getByTestId("scan-verification").textContent ?? "";
    expect(line).toContain("vault.scan.verification_detail ");
    expect(line).toContain("1.0 GiB");
    expect(line).toContain("2.0 GiB");
    expect(within(phase("verification")).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
    expect(screen.queryByTestId("scan-last-failed")).toBeNull();
  });

  it("leaves the verification's speed and time to the Vault's one expected finish (#755)", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true, scan_phase: "verification",
      verify_files_total: 22425, verify_files_done: 4495,
      verify_bytes_total: 78.5 * 1024 ** 3, verify_bytes_done: 25.3 * 1024 ** 3,
    }} />);
    const line = screen.getByTestId("scan-verification").textContent ?? "";
    expect(line).toContain("vault.scan.verification_detail ");
    expect(line).not.toContain("remaining");
  });

  it("presents an older source error below the running scan, with its cause translated", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true,
      last_error_source: "Source scan blocked: Visited directory changed during traversal: a/b",
      last_error_source_at: "2026-11-15T12:58:00+00:00",
      last_error_source_cause: "tree_changed",
    }} />);
    const status = screen.getByTestId("scan-status");
    const failed = screen.getByTestId("scan-last-failed");
    // Below the running scan, muted, and saying the scan resolves it.
    expect(status.lastElementChild).toBe(failed);
    expect(failed).toHaveClass("text-muted");
    expect(failed.textContent).toContain("vault.scan.previous_failed");
    expect(failed.textContent).toContain("vault.scan.cause.tree_changed");
    expect(failed.textContent).not.toContain("Visited directory changed");
    expect(failed.textContent).not.toContain("2026-11-15T12:58");
    expect(failed.textContent).toContain("vault.scan.previous_failed_resolving");
    expect(screen.getByTestId("scan-after").textContent).toBe("vault.scan.after_resolves");
  });

  it("keeps the stored text of an unknown cause", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: false, last_error_source: "Source scan: boom", last_error_source_at: "2026-11-15T12:58:00+00:00",
      last_error_source_cause: null,
    }} />);
    const failed = screen.getByTestId("scan-last-failed");
    expect(failed.textContent).toContain("Source scan: boom");
    expect(failed).toHaveClass("text-ink");
    expect(failed.textContent).not.toContain("previous_failed_resolving");
    expect(screen.queryByTestId("scan-in-progress")).toBeNull();
  });

  it("shows files seen and their rate during the traversal of a first adoption (#696)", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true, scan_phase: "traversal",
      traversal_files_seen: 96500, traversal_files_per_minute: 2144.4, traversal_files_expected: null,
    }} />);
    const line = screen.getByTestId("scan-traversal").textContent ?? "";
    expect(line).toContain("vault.scan.traversal_detail ");
    expect(line).toContain(`"files":"${formatNumber(96500)}","rate":"${formatNumber(2144)}"`);
    expect(line).not.toContain("remaining");
    expect(within(phase("traversal")).queryByRole("progressbar")).toBeNull(); // no total, no bar
  });

  it("estimates the traversal time left when a previous scan counted the Local Copies (#696)", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true, scan_phase: "traversal",
      traversal_files_seen: 96000, traversal_files_per_minute: 2000, traversal_files_expected: 138000,
    }} />);
    const line = screen.getByTestId("scan-traversal").textContent ?? "";
    expect(line).toContain("vault.scan.traversal_detail_estimate");
    // 42,000 files left at 2,000 per minute: 21 minutes.
    expect(line).toContain('vault.scan.remaining_minutes {\\"count\\":21}');
    expect(within(phase("traversal")).getByRole("progressbar")).toBeInTheDocument();
  });

  it("states the time left in hours beyond an hour (#696)", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true, scan_phase: "traversal",
      traversal_files_seen: 1000, traversal_files_per_minute: 1000, traversal_files_expected: 91000,
    }} />);
    expect(screen.getByTestId("scan-traversal").textContent).toContain('vault.scan.remaining_hours {\\"count\\":1.5}');
  });

  it("drops the traversal detail once the scan verifies (#696)", () => {
    render(<ScanStatus t={t} runtime={{
      scanning: true, scan_phase: "verification",
      traversal_files_seen: 138000, traversal_files_per_minute: 2000, traversal_files_expected: 138000,
      verify_files_total: 10, verify_files_done: 1, verify_bytes_total: 10, verify_bytes_done: 1,
    }} />);
    expect(screen.queryByTestId("scan-traversal")).toBeNull();
    expect(screen.getByTestId("scan-verification")).toBeInTheDocument();
  });

  it("shows how much of an unconfirmed Vault is already in the cloud (#711)", () => {
    render(<ScanStatus t={t} unconfirmed adoption={{ inCloud: 80000, total: 139256 }} runtime={{
      scanning: true, scan_phase: "verification",
    }} />);
    const adoption = screen.getByTestId("adoption-progress");
    expect(adoption.textContent).toContain("vault.adoption.in_cloud");
    expect(within(adoption).getByRole("progressbar").getAttribute("aria-valuenow")).toMatch(/^57\.4/);
    expect(screen.getByTestId("scan-after").textContent).toBe("vault.scan.after_confirms");
  });
});
