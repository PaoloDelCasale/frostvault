/** Theme tokens parsed from src/index.css and WCAG 2 contrast helpers (#653). */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const cssPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../src/index.css",
);
const css = readFileSync(cssPath, "utf8");

export const MINIMUM_CONTRAST = 4.5;

export type Palette = "light" | "dark";

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

export function paletteTokens(palette: Palette): Map<string, string> {
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

export function resolvedHex(tokens: Map<string, string>, token: string, seen = new Set<string>()): string {
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

export function relativeLuminance(hex: string): number {
  const channels = [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255);
  const [red, green, blue] = channels.map((channel) =>
    channel <= 0.04045
      ? channel / 12.92
      : ((channel + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

export function contrastRatio(foreground: string, background: string): number {
  const lighter = Math.max(relativeLuminance(foreground), relativeLuminance(background));
  const darker = Math.min(relativeLuminance(foreground), relativeLuminance(background));
  return (lighter + 0.05) / (darker + 0.05);
}
