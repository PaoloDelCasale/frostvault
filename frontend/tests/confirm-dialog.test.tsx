import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "@/components/ConfirmDialog";

describe("ConfirmDialog", () => {
  it("invokes onConfirm only on explicit confirmation, never on cancel or Esc", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onOpenChange = vi.fn();

    const { rerender } = render(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="Delete local copy"
        description="This removes the local file."
        confirmLabel="Delete local copy"
        onConfirm={onConfirm}
      />,
    );

    await screen.findByRole("alertdialog");

    await user.keyboard("{Escape}");
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onOpenChange).toHaveBeenCalledWith(false);

    onOpenChange.mockClear();
    rerender(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="Delete local copy"
        description="This removes the local file."
        confirmLabel="Delete local copy"
        onConfirm={onConfirm}
      />,
    );
    await screen.findByRole("alertdialog");

    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onOpenChange).toHaveBeenCalledWith(false);

    onOpenChange.mockClear();
    rerender(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="Delete local copy"
        description="This removes the local file."
        confirmLabel="Delete local copy"
        onConfirm={onConfirm}
      />,
    );
    await screen.findByRole("alertdialog");

    await user.click(screen.getByRole("button", { name: "Delete local copy" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });
  });
});
