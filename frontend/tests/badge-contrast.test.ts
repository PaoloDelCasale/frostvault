import { createElement } from "react";
import { render } from "@testing-library/react";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
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

const cssPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../src/index.css",
);
const css = readFileSync(cssPath, "utf8");

const MINIMUM_CONTRAST = 4.5;

type Palette = "light" | "dark";
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

function ruleBody(source: string, selector: string): string {
  const start = source.indexOf(selector);
  if (start < 0) throw new Error(`Missing CSS rule: ${selector}`);

  const open = source.indexOf("{", start + selector.length);
  let depth = 1;
  for (let index = open + 1; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(open + 1, index);
  }
  throw new Error(`Unclosed CSS rule: ${selector}`);
}

function declarations(rule: string): Map<string, string> {
  return new Map(
    [...rule.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((match) => [
      match[1],
      match[2].trim(),
    ]),
  );
}

function paletteTokens(palette: Palette): Map<string, string> {
  const tokens = new Map([
    ...declarations(ruleBody(css, "@theme")),
    ...declarations(ruleBody(css, ":root")),
  ]);
  if (palette === "dark") {
    for (const [token, value] of declarations(ruleBody(css, '[data-theme="dark"]'))) {
      tokens.set(token, value);
    }
  }
  return tokens;
}

function resolvedHex(tokens: Map<string, string>, token: string, seen = new Set<string>()): string {
  if (seen.has(token)) throw new Error(`Circular CSS custom property: ${token}`);
  const value = tokens.get(token);
  if (!value) throw new Error(`Missing CSS custom property: ${token}`);

  const alias = value.match(/^var\((--[\w-]+)\)$/);
  if (alias) return resolvedHex(tokens, alias[1], new Set([...seen, token]));
  if (!/^#[\da-f]{6}$/i.test(value)) {
    throw new Error(`${token} must resolve to a six-digit hex colour, received ${value}`);
  }
  return value;
}

function relativeLuminance(hex: string): number {
  const channels = [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255);
  const [red, green, blue] = channels.map((channel) =>
    channel <= 0.04045
      ? channel / 12.92
      : ((channel + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrastRatio(foreground: string, background: string): number {
  const lighter = Math.max(relativeLuminance(foreground), relativeLuminance(background));
  const darker = Math.min(relativeLuminance(foreground), relativeLuminance(background));
  return (lighter + 0.05) / (darker + 0.05);
}

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
