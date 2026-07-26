import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "@/App";

describe("App shell screen", () => {
  it("renders the vault shell heading", () => {
    render(<App />);
    expect(
      screen.getByRole("heading", { name: "Test Archive" }),
    ).toBeInTheDocument();
  });
});
