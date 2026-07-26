import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Badge, BADGE_STATE_LABELS, type BadgeState } from "@/components/Badge";

const STATES: BadgeState[] = [
  "both",
  "local_only",
  "cloud_only",
  "restoring",
  "mixed",
  "missing",
  "unsupported",
];

describe("Badge", () => {
  it("renders a text label for each of the seven states, not just a colour class", () => {
    for (const state of STATES) {
      const { unmount } = render(<Badge state={state} />);
      expect(screen.getByText(BADGE_STATE_LABELS[state])).toBeInTheDocument();
      unmount();
    }
  });
});
