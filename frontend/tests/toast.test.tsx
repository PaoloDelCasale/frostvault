import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Toast, ToastRegion } from "@/components/Toast";
import { notify, TOAST_SUCCESS_MS } from "@/components/toastQueue";

describe("Toast", () => {
  it("success is a polite status, error an alert, without mixing aria-live (#656)", () => {
    const { rerender } = render(
      <Toast message="Scan complete" variant="success" closeLabel="Chiudi notifica" onClose={() => undefined} />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Scan complete");
    expect(screen.getByRole("status")).not.toHaveAttribute("aria-live");

    rerender(
      <Toast message="Upload failed" variant="error" closeLabel="Chiudi notifica" onClose={() => undefined} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Upload failed");
    expect(screen.getByRole("alert")).not.toHaveAttribute("aria-live");
    expect(screen.getByRole("button", { name: "Chiudi notifica" })).toHaveClass("size-11");
  });
});

describe("ToastRegion queue (#656)", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("shows one toast at a time and closes successes by itself", () => {
    render(<ToastRegion closeLabel="Close notification" />);
    act(() => { notify("Paused"); notify("Saved"); notify("Saved"); });
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("Paused");

    act(() => { vi.advanceTimersByTime(TOAST_SUCCESS_MS); });
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
    act(() => { vi.advanceTimersByTime(TOAST_SUCCESS_MS); });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("keeps an error until it is dismissed, then shows the next one", () => {
    render(<ToastRegion closeLabel="Close notification" />);
    act(() => { notify("Pause failed", "error"); notify("Resumed"); });
    act(() => { vi.advanceTimersByTime(TOAST_SUCCESS_MS * 3); });
    expect(screen.getByRole("alert")).toHaveTextContent("Pause failed");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    act(() => { screen.getByRole("button", { name: "Close notification" }).click(); });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Resumed");
  });
});
