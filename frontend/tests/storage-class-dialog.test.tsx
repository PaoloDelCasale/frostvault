import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { JobProgress } from "@/pages/archive/JobProgress";
import { StorageClassDialog } from "@/pages/archive/StorageClassDialog";
import type { StorageClassOption } from "@/pages/archive/storageClassOptions";
import type { JobGroup } from "@/api/types";

const catalog: Record<string, string> = {
  "ui.storage_class_confirm_title": "Change storage class",
  "ui.storage_class_confirm_body":
    "Change {count} cloud object(s) ({bytes}) from their current class to {target}.",
  "ui.storage_class_picker_label": "Target storage class",
  "ui.storage_class_confirm_warning": "Cold class warning",
  "ui.storage_class_policy_note": "Policy note",
  "ui.storage_class_restore_warning":
    "Current class needs S3 restore (~{hours}h, ~€{cost}) before the change. RestoreObject cannot be cancelled.",
  "ui.storage_class_pin_after": "Pin path from lifecycle after change",
  "ui.storage_class_option_instant":
    "{id} — €{rate}/GiB·mo · Instant retrieval · Recovery —",
  "ui.storage_class_option_restore":
    "{id} — €{rate}/GiB·mo · Restore ~{hours}h · Recovery €{restore_rate}/GiB",
  "ui.storage_class_rate": "€{rate}/GiB·mo",
  "ui.storage_class_retrieval_instant": "Instant retrieval",
  "ui.storage_class_retrieval_restore": "Restore ~{hours}h",
  "ui.storage_class_recovery_price": "Recovery €{restore_rate}/GiB",
  "ui.storage_class_recovery_none": "Recovery —",
  "ui.row_action_storage_class": "Change storage class…",
  "ui.cancel": "Cancel",
  "ui.job_bytes_progress": "{transferred} of {total}",
  "ui.job_files_progress": "{completed} of {total} files",
  "ui.stop": "Stop",
  "operation.failed": "Failed",
  "operation.restoring": "Restoring",
  "ui.approving": "Approving…",
  "ui.approve_restore": "Approve",
  "ui.stopping": "Stopping…",
};

function t(key: string, params?: Record<string, string | number>): string {
  let value = catalog[key] ?? key;
  if (params) {
    for (const [name, raw] of Object.entries(params)) {
      value = value.replace(`{${name}}`, String(raw));
    }
  }
  return value;
}

const options: StorageClassOption[] = [
  {
    id: "STANDARD",
    currency: "EUR",
    storage_rate_eur_per_gib_month: 0.023,
    retrieval: "instant",
    min_duration_days: 0,
    requires_restore: false,
    availability_zones: "multi",
  },
  {
    id: "DEEP_ARCHIVE",
    currency: "EUR",
    storage_rate_eur_per_gib_month: 0.00099,
    retrieval: "restore",
    min_duration_days: 180,
    requires_restore: true,
    availability_zones: "multi",
    restore_hours_bulk: 48,
    restore_rate_eur_per_gib_bulk: 0.0025,
  },
];

describe("StorageClassDialog descriptions (seam 6)", () => {
  it("shows a browser dropdown with rate, retrieval, and recovery price", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <StorageClassDialog
        open
        onOpenChange={() => {}}
        path="report.txt"
        count={1}
        totalBytes={1024 ** 3}
        currentClass="DEEP_ARCHIVE"
        restoreState={null}
        classOptions={options}
        restoreEstimate={{ hours: 48, costEur: 0.0025 }}
        t={t}
        onConfirm={onConfirm}
      />,
    );

    const picker = screen.getByTestId("storage-class-picker");
    expect(picker.tagName).toBe("BUTTON");
    expect(picker).toHaveTextContent(/€0.023\/GiB/);
    expect(picker).toHaveTextContent(/Instant retrieval/);
    expect(picker).toHaveTextContent(/Recovery —/);
    expect(picker).not.toHaveTextContent(/Min\s+\d/);

    await user.click(picker);
    const menu = await screen.findByTestId("storage-class-picker-menu");
    expect(within(menu).getByText("DEEP_ARCHIVE")).toBeInTheDocument();
    expect(within(menu).getByText(/€0.00099\/GiB/)).toBeInTheDocument();
    expect(within(menu).getByText(/Restore ~48h/)).toBeInTheDocument();
    expect(within(menu).getByText(/Recovery €0.0025\/GiB/)).toBeInTheDocument();
    expect(menu).not.toHaveTextContent(/Min\s+\d/);
    expect(menu).not.toHaveTextContent(/180/);

    expect(screen.getByTestId("storage-class-restore-warning")).toHaveTextContent(
      /48h/,
    );

    await user.click(screen.getByTestId("storage-class-confirm"));
    expect(onConfirm).toHaveBeenCalledWith("STANDARD", { pinAfter: false });
  });
});

describe("JobProgress message visibility (seam 7)", () => {
  it("shows job.message for failed storage-class jobs", () => {
    const job: JobGroup = {
      id: "g1",
      path: "report.txt",
      action: "storage-class",
      status: "failed",
      message:
        "Restore the Archive Version before changing storage class from DEEP_ARCHIVE",
      message_key: "job.storage_class_needs_restore",
      total_bytes: 12,
      transferred_bytes: 0,
      item_count: 1,
      completed_count: 0,
      failed_count: 1,
      cancelled_count: 0,
      percent: 0,
    };
    render(
      <JobProgress
        job={job}
        t={t}
        canCancel={false}
        canApprove={false}
        onCancel={() => {}}
        onApprove={() => {}}
      />,
    );
    expect(
      screen.getByText(/Restore the Archive Version before changing/),
    ).toBeInTheDocument();
  });

  it("shows job.message while storage-class is restoring", () => {
    const job: JobGroup = {
      id: "g2",
      path: "report.txt",
      action: "storage-class",
      status: "restoring",
      message: "Waiting for DEEP_ARCHIVE restore before changing storage class",
      message_key: "job.storage_class_restoring",
      total_bytes: 12,
      transferred_bytes: 0,
      item_count: 1,
      completed_count: 0,
      failed_count: 0,
      cancelled_count: 0,
      percent: 0,
      estimated_hours: 48,
    };
    render(
      <JobProgress
        job={job}
        t={t}
        canCancel
        canApprove={false}
        onCancel={() => {}}
        onApprove={() => {}}
      />,
    );
    expect(
      screen.getByText(/Waiting for DEEP_ARCHIVE restore/),
    ).toBeInTheDocument();
  });
});
