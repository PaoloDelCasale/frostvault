import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { BottomSheet } from "@/components/BottomSheet";

describe("BottomSheet", () => {
  it("closes on action selection and reports the chosen action", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    const onOpenChange = vi.fn();

    render(
      <BottomSheet
        open
        onOpenChange={onOpenChange}
        title="Row actions"
        actions={[
          { id: "upload", label: "Upload to cloud" },
          { id: "delete-local", label: "Delete local copy", tone: "danger" },
        ]}
        onAction={onAction}
      />,
    );

    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "Delete local copy" }));

    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction).toHaveBeenCalledWith("delete-local");
    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });
  });
});
