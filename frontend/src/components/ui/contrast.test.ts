/**
 * The palette, measured rather than eyeballed.
 *
 * Every colour pair in this file is one that a reader actually has to
 * separate: text against the surface it sits on, a label against its own
 * button fill, a border against the panel it encloses. The floors are WCAG
 * 2.1 -- 4.5:1 for text under 18px, 3:1 for large text and for non-text UI
 * (1.4.11) -- with one deliberate exception, marked below.
 *
 * It exists because four of these pairs were failing and nothing said so. A
 * palette is the kind of thing that gets adjusted by eye at 2am on a bright
 * monitor, and by eye is exactly how a 1.24:1 border gets shipped.
 *
 * The ratios are recomputed from `globals.css` at test time, so editing a
 * token is what moves them. Hard-coding the expected numbers here would only
 * assert that this file agrees with itself.
 */
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const CSS = readFileSync(join(__dirname, "../../app/globals.css"), "utf8");

/**
 * Reads a `--token: #rrggbb;` declaration out of the stylesheet. Parsed by
 * hand rather than by regex so the escaping survives a heredoc, and so a
 * token that is defined only as an alias fails loudly instead of matching
 * something else.
 */
function token(name: string): string {
  for (const line of CSS.split("\n")) {
    const decl = line.trim();
    if (!decl.startsWith(`--${name}:`)) continue;
    const value = decl.slice(name.length + 3).trim();
    const hex = value.split(";")[0].trim().split(" ")[0];
    if (/^#[0-9a-fA-F]{6}$/.test(hex)) return hex;
  }
  throw new Error(`--${name} is not defined as a hex literal in globals.css`);
}

function channel(c: number): number {
  const s = c / 255;
  return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

function luminance(hex: string): number {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

/** [foreground, background, floor, what it is] */
type Pair = [string, string, number, string];

const TEXT: Pair[] = [
  ["ink", "surface", 4.5, "body text on a panel"],
  ["ink", "plane", 4.5, "body text on the page"],
  ["ink-2", "surface", 4.5, "secondary text on a panel"],
  ["muted", "surface", 4.5, "labels, hints, captions"],
  ["muted", "plane", 4.5, "labels on the page"],
  ["accent", "surface", 4.5, "links"],
  ["good", "surface", 4.5, "the good status word"],
  ["warning", "surface", 4.5, "the warning status word"],
  ["serious", "surface", 4.5, "the serious status word"],
  ["critical", "surface", 4.5, "error text, and the reason --critical moved"],
  ["critical", "plane", 4.5, "error text on the page"],
  ["ink", "baseline", 4.5, "the level chip on an Unavailable notice"],
];

const FILLS: Pair[] = [
  ["plane", "accent", 4.5, "the primary button label"],
  // Also the LIVE-ARMED mode badge, which is the one string in this app that
  // has to survive a bad monitor.
  ["plane", "critical", 4.5, "the danger button label and the LIVE badge"],
];

const NON_TEXT: Pair[] = [
  ["baseline", "surface", 3.0, "axes and strong dividers (WCAG 1.4.11)"],
];

describe("palette contrast", () => {
  it.each(TEXT)("%s on %s clears %d — %s", (fg, bg, floor) => {
    expect(contrast(token(fg), token(bg))).toBeGreaterThanOrEqual(floor);
  });

  it.each(FILLS)("%s on %s clears %d — %s", (fg, bg, floor) => {
    expect(contrast(token(fg), token(bg))).toBeGreaterThanOrEqual(floor);
  });

  it.each(NON_TEXT)("%s on %s clears %d — %s", (fg, bg, floor) => {
    expect(contrast(token(fg), token(bg))).toBeGreaterThanOrEqual(floor);
  });

  /**
   * `--line` is the one deliberate exception to 1.4.11's 3:1. It is not a
   * control boundary that has to be found; it is the hairline between table
   * rows, and at 3:1 a dense table becomes a grid of cages. The floor is set
   * where it stops being invisible. What it must never do again is sit at
   * 1.24:1, where it was decorative in the sense of having no visible effect.
   */
  it("--line is a hairline you can actually see", () => {
    const r = contrast(token("line"), token("surface"));
    expect(r).toBeGreaterThanOrEqual(1.7);
    expect(r).toBeLessThan(3.0);
  });

  /** Raised rows, inputs and the active nav item all rest on this. */
  it("--surface-2 is distinguishable from the panel it sits on", () => {
    expect(contrast(token("surface-2"), token("surface"))).toBeGreaterThanOrEqual(1.3);
  });

  /**
   * Panels separate by edge and shadow because they cannot separate by fill:
   * this asserts the ceiling that forced that decision, so a future attempt to
   * fix panel separation by lifting the surface finds the reason it will not
   * work written down as a test rather than as a comment.
   */
  it("fill alone cannot separate a panel from the page", () => {
    expect(contrast(token("surface"), "#000000")).toBeLessThan(1.35);
  });
});

/**
 * Everything above measures the palette. This measures whether the palette is
 * the thing actually being used.
 *
 * Five components had reached past it for Tailwind's stock colours -- 39 uses
 * of `text-neutral-*`, `text-amber-*`, `text-red-*`, `border-neutral-800`.
 * Two of those were the failures this whole file exists to catch, and it could
 * not have caught either, because a colour that never appears in `globals.css`
 * is not a colour these tests can see: `text-neutral-500` measured 3.67:1
 * against the panel, under the 4.5 floor for body text, and
 * `border-neutral-800` measured 1.15:1 -- invisible, the same defect `--line`
 * had.
 *
 * So the guard is structural rather than numeric. Measuring the tokens is not
 * enough on its own if a component can simply decline to use them.
 */
describe("the palette is the palette", () => {
  const SOURCE = join(__dirname, "../..");

  /**
   * Tailwind's stock scales, plus bare `black` and `white`. Ours are all
   * named, so a digit here is always a colour that skipped the design system;
   * `black` and `white` are caught too because pure black beside this app's
   * near-black `--plane` is a difference you can see without being able to
   * name it.
   *
   * `transparent`, `current` and `inherit` are keywords rather than colour
   * choices -- `border-transparent` is how the nav reserves space for its
   * active marker -- so they are deliberately not matched.
   */
  const STOCK =
    /\b(?:text|bg|border|ring|fill|stroke|from|via|to|divide|outline|shadow|decoration|accent|caret|placeholder)-(?:(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}|black|white)\b/;

  function sources(dir: string): string[] {
    const out: string[] = [];
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) out.push(...sources(full));
      else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full);
    }
    return out;
  }

  it("no component reaches past it for a stock Tailwind colour", () => {
    const offenders: string[] = [];
    for (const file of sources(SOURCE)) {
      for (const [i, line] of readFileSync(file, "utf8").split("\n").entries()) {
        const hit = line.match(STOCK);
        if (hit) offenders.push(`${file.replace(SOURCE, "src")}:${i + 1}  ${hit[0]}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
