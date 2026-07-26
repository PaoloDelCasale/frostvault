import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "@/App";

describe("App shell screen", () => {
  it("renders the vault shell heading", () => {
    render(<App />);
    expect(
      screen.getByRole("heading", { level: 1, name: "Test Archive" }),
    ).toBeInTheDocument();
  });

  it("renders archive statistics and the first file without burying it", () => {
    render(<App />);
    expect(screen.getByTestId("stats-compact")).toBeInTheDocument();
    expect(screen.getByTestId("archive-file-list")).toHaveTextContent(
      "reports/q1-summary.pdf",
    );
    expect(screen.getByTestId("safety-footer")).toBeInTheDocument();
  });
});
