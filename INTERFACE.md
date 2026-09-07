# INTERFACE.md

The look of the app, and which parts of it are measured. 2026-09-06.

---

## The one rule

**A colour pair is measured, not judged by eye.** `frontend/src/components/ui/contrast.test.ts`
recomputes every ratio in this document from `globals.css` at test time and
fails if one drops below its floor.

This exists because four pairs were failing and nothing said so. A palette is
the kind of thing that gets nudged by eye on a bright monitor, and by eye is
exactly how a 1.24:1 border gets shipped.

## What was measured, and what moved

Ratios are WCAG 2.1 relative luminance. The floor is 4.5:1 for text under
18px, 3:1 for non-text UI (1.4.11).

| token | was | now | why |
|---|---|---|---|
| `--line` | 1.24:1 | **1.87:1** | every panel edge and row divider in the app |
| `--surface-2` | 1.09:1 | **1.35:1** | inputs, raised rows, the active nav item |
| `--baseline` | 1.48:1 | **3.01:1** | axes and strong dividers |
| `--critical` | 3.62:1 | **4.50:1** | error text was below the body-text floor |

Two more failures were fixed without touching the palette, by changing the
label instead of the fill:

| surface | was | now |
|---|---|---|
| primary button label | white on accent, 3.64:1 | page colour on accent, **5.34:1** |
| LIVE·ARMED badge | white on red, 3.87:1 | page colour on red, **5.02:1** |

Darkening the accent instead would have fixed the button and broken every link,
which reads at 4.79:1 and has no margin to give.

## Panels separate by edge and shadow, never by fill

The panel surface is 1.12:1 against the page, and it cannot usefully be lifted:
even a **pure black** page reaches only 1.21:1 against it. Chasing separation
by fill would have greyed out the whole terminal for nothing.

So the border does the work — which is why `--line` mattered enough to move —
with a shadow underneath it. There is a test asserting the 1.21 ceiling, so the
next person to try fixing panel separation by lifting the surface finds the
reason it will not work written as a test rather than as a comment.

## The one deliberate exception

`--line` is held **between 1.7 and 3.0**, below what 1.4.11 asks of a control
boundary. It is not a boundary anybody has to find; it is the hairline between
table rows, and at 3:1 a dense table becomes a grid of cages. The floor is set
where it stops being invisible, and the ceiling stops it becoming a cage.

## Below 768px there was no navigation at all

The sidebar is `hidden md:flex` and nothing replaced it, so on a phone every
route was reachable only by typing its URL. That is not a styling gap — it is
the application being unusable on a phone, which is what a desktop-only review
never sees.

`MobileNav` renders `NavList`, the **same** list the desktop rail renders.
Extracted rather than copied: two lists over the same `NAV` would agree until
the first route was added to one of them, and the one that drifts is always the
one fewer people open.

The top bar sheds its right-hand cluster as it narrows — clock, then API
version, then session. **The mode badge never hides.** Which mode this is, is
the one thing on that bar nobody may have to guess at.

## The front page was several levels behind the platform

It rendered eight fixed "not built yet" notices. Four had stopped being true:
positions, orders, bots and the trade journal are all served, and all four
already had working components on their own pages.

Understating what exists is not the safe direction of that error. It is the
same screen-disagrees-with-system failure as overstating it, and it is the one
nobody files a bug about. Each panel now renders the component that owns the
question, so the next notice to stop being true corrects itself instead of
waiting for someone to notice the sentence.

Two notices remain because they are still accurate: there is no live quote
subscription, and the research reports are not served read-only.

`DashboardAccount` keeps the rule `PortfolioDashboard` already states — **a
dash is never a zero** — and shows one named account rather than a sum, because
a paper balance and a demo balance are not one number.

## The type scale, and the hierarchy it had backwards

It was three arbitrary sizes doing five jobs: `text-[10px]` (53 uses),
`text-[11px]` (192) and `text-xs`/12px (54). Above them, `text-sm` was used
twelve times in the whole app.

The size was the smaller problem. **`text-[11px]` was mostly prose** — the
notes, the authority statements, the reason a panel is unavailable. The
sentences a reader most has to read carefully were set *smaller* than the table
data beside them. That is the hierarchy backwards, and no amount of contrast
fixes it.

Three named steps, each with one job:

| step | px | job |
|---|---|---|
| `text-micro` | 11 | badges, level tags, the legend — read in bursts |
| `text-mini` | 12 | uppercase tracked labels, table headers |
| `text-body` | 13 | prose, notes, hints, table cells |

`text-sm` (14) upward is unchanged: nav, buttons, inputs, titles, the stat hero.

The sweep was applied **by role, not by size**. An uppercase tracked run is a
label whatever pixel value it was written at, so it went to `text-mini` whether
it started at 10px or 11px; everything else that was 10px is a badge or a tag
and went to `text-micro`; the rest went to `text-body`. 299 sites, and the
decision was made per class list rather than per token.

## The palette was not the palette

Five components had reached past it for Tailwind's stock colours — 39 uses of
`text-neutral-*`, `text-amber-*`, `text-red-*`, `border-neutral-800`, plus
`text-black` and a `bg-black/60` backdrop.

**Two of those were the exact failures the contrast test exists to catch, and
it could not have caught either.** A colour that never appears in `globals.css`
is not a colour those tests can see:

| off-palette | measured | mapped to | now |
|---|---|---|---|
| `text-neutral-500` | **3.67:1** — under the body floor | `text-muted` | 4.85:1 |
| `border-neutral-800` | **1.15:1** — invisible | `border-line` | 1.87:1 |

The other 37 were consistency rather than contrast — including `text-black`,
which measures 5.43:1 and 11.45:1 on its two fills and passes comfortably. It
moved to `text-plane` because every other dark-on-fill label in the app uses
that token, and pure black beside a near-black is a difference you see without
being able to name.

So there is now a **structural** guard as well as a numeric one: a test walks
every source file and fails on any stock Tailwind colour utility, naming the
file and line. Measuring the tokens is not enough on its own if a component can
simply decline to use them. `transparent`, `current` and `inherit` are exempt —
they are keywords, not colour choices, and `border-transparent` is how the nav
reserves space for its active marker.

## Conventions

- `StatTile` renders `null` and `undefined` as `—`. Never pass `0` for absent.
- Status colours are for state and always ship with a label. Series colours are
  for data only.
- Loading, empty and error are three different states. `EmptyState` says
  "nothing here", never "all clear".
- One focus ring, defined once in `globals.css`, offset 2px off the control's
  own border. Without the offset it lands on the 1px edge and reads as a
  slightly thicker border rather than as focus.
- Motion is decoration here — every transition is a colour or a 1px shift — so
  `prefers-reduced-motion` turns all of it off and costs nothing.

## Checks

```bash
cd frontend
npx vitest run          # includes the contrast test
npx tsc --noEmit
npx eslint
npx next build
```
