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
          {
            id: "delete-local",
            label: "Delete local copy",
            description: "Cloud stays recoverable",
            tone: "danger",
          },
        ]}
        onAction={onAction}
      />,
    );

    await screen.findByRole("dialog");
    expect(screen.getByText("Cloud stays recoverable")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Delete local copy/i }));

    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onAction).toHaveBeenCalledWith("delete-local");
    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });
  });

  it("wraps long action descriptions instead of clipping them", async () => {
    const longHint =
      "Deletes every Archive Version and Delete Marker in the cloud. Local files on the server are not deleted.";

    render(
      <BottomSheet
        open
        onOpenChange={vi.fn()}
        title="Actions for .FP002933.JPG.verify-very-long-filename-example"
        actions={[
          {
            id: "cloud-purge",
            label: "Purge from cloud permanently",
            description: longHint,
            tone: "danger",
          },
          {
            id: "free-space",
            label: "Free local space",
            description:
              "Removes only the Local Copy. Cloud Archive Versions stay recoverable.",
          },
        ]}
        onAction={vi.fn()}
      />,
    );

    await screen.findByRole("dialog");

    const title = screen.getByRole("heading", {
      name: /Actions for \.FP002933\.JPG\.verify-very-long-filename-example/,
    });
    expect(title.className).toMatch(/break-words/);

    const purge = screen.getByRole("button", {
      name: /Purge from cloud permanently/i,
    });
    expect(purge.className).toMatch(/whitespace-normal/);
    expect(purge).toHaveTextContent(longHint);

    const hint = screen.getByText(longHint);
    expect(hint.className).toMatch(/text-sm/);
    expect(hint.className).toMatch(/text-white/);

    const freeHint = screen.getByText(
      "Removes only the Local Copy. Cloud Archive Versions stay recoverable.",
    );
    expect(freeHint.className).toMatch(/text-ink/);
    expect(freeHint.className).not.toMatch(/text-muted/);
  });
});
