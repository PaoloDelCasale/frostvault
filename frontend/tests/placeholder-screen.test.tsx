import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PlaceholderScreen } from "@/components/PlaceholderScreen";

describe("PlaceholderScreen", () => {
  it("renders the FrostVault placeholder heading", () => {
    render(<PlaceholderScreen />);
    expect(
      screen.getByRole("heading", { name: "FrostVault" }),
    ).toBeInTheDocument();
  });
});
