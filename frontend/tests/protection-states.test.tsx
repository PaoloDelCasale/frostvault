import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ArchiveListItem } from "@/api/types";
import { Badge } from "@/components/Badge";
import { itemStateBadge } from "@/pages/archive/protectionLabel";

const translations: Record<string, string> = {
  "state.pending": "In attesa", "state.protected": "Protetto",
  "state.problem": "Problema", "state.unconfirmed": "Non accertato",
  "state.cloud_only": "Solo cloud", "state.excluded": "Escluso", "state.unsupported": "Non supportato",
  "state.stopped": "Fermato da te",
  "state.last_known": "Ultimo stato noto: {state} ({date})",
  "state.previous_snapshot_protected": "Versione precedente protetta",
};
function t(key: string, params?: Record<string, string | number>) {
  return Object.entries(params || {}).reduce((text, [key, value]) => text.replaceAll(`{${key}}`, String(value)), translations[key] || key);
}
function file(state: string): ArchiveListItem {
  return { type: "file", name: "photo.jpg", path: "photo.jpg", state };
}

describe("protection-state labels", () => {
  it.each(["pending", "protected", "problem", "unconfirmed", "cloud_only", "stopped", "excluded", "unsupported"])("renders %s with a localized textual label", (state) => {
    const badge = itemStateBadge(file(state), t);
    render(<Badge state={badge.state} label={badge.label} />);
    expect(screen.getByText(translations[`state.${state}`])).toBeInTheDocument();
  });

  it("keeps an excluded file distinguishable from a pending one", () => {
    const excluded = itemStateBadge(file("excluded"), t);
    const pending = itemStateBadge(file("pending"), t);
    expect(excluded.state).toBe("excluded");
    expect(excluded.label).not.toBe(pending.label);
    expect(excluded.label).toBe("Escluso");
  });

  it("keeps a Stopped Upload distinguishable from a pending one (#630)", () => {
    const stopped = itemStateBadge(file("stopped"), t);
    expect(stopped.state).toBe("stopped");
    expect(stopped.label).toBe("Fermato da te");
    expect(stopped.label).not.toBe(itemStateBadge(file("pending"), t).label);
  });

  it("keeps last evidence and its date visible when the volume is absent", () => {
    const badge = itemStateBadge({ ...file("unconfirmed"), last_known_state: "protected", last_known_at: "2026-11-03" }, t);
    expect(badge.label).toBe("Non accertato · Ultimo stato noto: Protetto (2026-11-03)");
  });

  it("does not claim the changed bytes are protected by a previous snapshot", () => {
    const badge = itemStateBadge({ ...file("pending"), previous_snapshot_protected: true }, t);
    expect(badge.state).toBe("pending");
    expect(badge.label).toBe("In attesa · Versione precedente protetta");
  });
});
