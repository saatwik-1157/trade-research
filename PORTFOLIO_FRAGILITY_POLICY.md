# PORTFOLIO_FRAGILITY_POLICY.md

L66 sections 22, 23 and 24. Where the portfolio is brittle.

---

## Not built, and the reason is data

Fragility analysis identifies strategies contributing disproportionate risk,
highly correlated strategies, hidden concentration and synchronized losses.

Every one of those is a **cross-instrument** measurement. This deployment holds
700 H1 bars for one symbol and one open position.

`app/portfolio/exposure.py` has reported correlation as unavailable since L53
and still does. Section 24 says to reuse the existing correlation
infrastructure; the existing correlation infrastructure's answer is that it
cannot run, and that answer is correct.

## What the platform can honestly say instead

The currency breakdown, which `exposure.py` already computes and already names
as the closest available proxy: several USD-long positions show up as one large
USD figure, which is the common risk factor -- measured from recorded symbol
metadata rather than estimated from prices nobody has.

That is a real measurement of a related quantity, offered as such.

## A correction made at L66

`correlation_note()` said `market_bars` was empty. It now holds 700 rows, all
one instrument. **The verdict was right and the reason had gone stale**, and a
reader checking whether the situation had changed would have been told the
wrong thing.

Fixed. The test that pinned the old wording was also fixed -- it required the
string `market_bars` in the reason, so it blocked the reason being corrected. It
now asserts the property (unavailable, with a substantive reason and a named
substitute) rather than the sentence.

## Not automatic

Section 22's last line: do not automatically remove strategies. Nothing here
removes anything -- every `Recommendation` is TIGHTENS or NEUTRAL, and none of
them executes.
