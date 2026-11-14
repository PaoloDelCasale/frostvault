import { createElement } from "react";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  BADGE_STATE_VARIANT_CLASSES,
  Badge,
  type BadgeState,
} from "@/components/Badge";
import {
  STORAGE_BADGE_VARIANT_CLASSES,
  StorageBadge,
  type StorageClass,
} from "@/components/StorageBadge";

import { MINIMUM_CONTRAST, contrastRatio, paletteTokens, resolvedHex } from "./theme-tokens";

type BadgeVariant =
  | { kind: "state"; value: BadgeState }
  | { kind: "storage"; value: StorageClass };
type BadgePair = {
  badge: string;
  foreground: string;
  background: string;
};

const BADGE_VARIANTS: readonly BadgeVariant[] = [
  ...(Object.keys(BADGE_STATE_VARIANT_CLASSES) as BadgeState[]).map((value) => ({
    kind: "state" as const,
    value,
  })),
  ...(Object.keys(STORAGE_BADGE_VARIANT_CLASSES) as StorageClass[]).map((value) => ({
    kind: "storage" as const,
    value,
  })),
];

function tokenFromClassName(className: string, utility: "bg" | "text"): string {
  const arbitraryToken = className.match(
    new RegExp(`${utility}-\\[var\\((--[\\w-]+)\\)\\]`),
  );
  if (arbitraryToken) return arbitraryToken[1];

  if (utility === "bg") {
    const namedToken = className.match(new RegExp(`(?:^|\\s)${utility}-([\\w-]+)(?:\\s|$)`));
    if (namedToken) return `--${namedToken[1]}`;
  }

  throw new Error(`Missing ${utility} custom-property class in: ${className}`);
}

function renderedBadgePair(variant: BadgeVariant): BadgePair {
  const result = variant.kind === "state"
    ? render(createElement(Badge, { state: variant.value }))
    : render(createElement(StorageBadge, { storage: variant.value }));
  const element = result.container.firstElementChild;
  if (!(element instanceof HTMLElement)) {
    throw new Error(`Missing rendered ${variant.kind} badge`);
  }

  const mappedClasses = variant.kind === "state"
    ? BADGE_STATE_VARIANT_CLASSES[variant.value]
    : STORAGE_BADGE_VARIANT_CLASSES[variant.value];
  expect(
    element,
    `${variant.value}: rendered classes must include its production variant mapping`,
  ).toHaveClass(...mappedClasses.split(" "));

  return {
    badge: variant.value,
    foreground: tokenFromClassName(element.className, "text"),
    background: tokenFromClassName(element.className, "bg"),
  };
}

const CASES = (["light", "dark"] as const).flatMap((palette) =>
  BADGE_VARIANTS.map((variant) => ({ palette, variant, badge: variant.value })),
);

describe("badge palette contrast", () => {
  // Badge labels are normal text (12–13px bold), so WCAG 2 AA requires 4.5:1.
  it.each(CASES)(
    "$palette $badge foreground/background meets WCAG AA",
    ({ palette, variant }) => {
      const productionPair = renderedBadgePair(variant);
      const tokens = paletteTokens(palette);
      const foregroundHex = resolvedHex(tokens, productionPair.foreground);
      const backgroundHex = resolvedHex(tokens, productionPair.background);
      const ratio = contrastRatio(foregroundHex, backgroundHex);

      expect(
        ratio,
        `${palette} ${variant.value}: ${foregroundHex} on ${backgroundHex} has contrast ${ratio.toFixed(3)}:1`,
      ).toBeGreaterThanOrEqual(MINIMUM_CONTRAST);
    },
  );
});
