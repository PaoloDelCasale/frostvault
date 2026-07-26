import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Toast } from "@/components/Toast";

describe("Toast", () => {
  it('exposes role="status" for success and role="alert" for errors', () => {
    const { rerender } = render(
      <Toast open message="Scan complete" variant="success" onClose={() => undefined} />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Scan complete");
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");

    rerender(
      <Toast open message="Upload failed" variant="error" onClose={() => undefined} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Upload failed");
    expect(screen.getByRole("alert")).toHaveAttribute("aria-live", "polite");
  });
});
