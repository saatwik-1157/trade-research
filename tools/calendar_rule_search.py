#!/usr/bin/env python
"""Trade the day-of-week effect, through the full pipeline, with controls.

`calendar_search.py` tabulated a day-of-week effect on the FX majors:
Thursday -0.0183% a day and Friday -0.0222%, negative in all four era blocks
and both out-of-sample halves, with a shuffled-label null at p = 0.0002. That
is a table of returns, not a strategy. This file asks the only question that
matters next: **does it survive being traded** -- entered at a bar's open,
bracketed at 1.5xATR, charged this broker's measured spread, and judged by
`rule_search`'s own gates rather than by a mean.

Every result this repository has ever liked has died between the table and
the trade. `donchian_fade_55` showed +227 points and a pooled t of 2.79 and
dissolved under era blocks and date clustering; the H4 exit grid looked like
an exit effect and was one directional era seen from inside a grid.

THE CONTROLS ARE THE POINT, as they were in the volume search. Tuesday and
Wednesday showed no effect (t 0.95 and 1.41), so a rule that shorts them
should NOT work. If the Tuesday control scores like the Thursday rule, the
pipeline is measuring exposure or cost rather than the calendar, and the
whole finding is an artefact. `volume_search.py` only caught its own null
because the control was in the run.

Direction is expressed per pair as written, NOT normalised to the foreign
currency. That is deliberate and it is a limitation: the tabulated effect is
a basket of seven pairs quoted inconsistently -- four with USD as the counter
currency, three with USD as the base -- so "short on Friday" means short the
pair as quoted, and a dollar effect and a risk effect would not be
distinguishable. `cross_search.py` holds the normalisation that fixes this.
The per-symbol output is where to look before believing any pooled figure.

    python tools/calendar_rule_search.py --timeframe D1 --years 10
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
import rule_search  # noqa: E402

DAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4}


def weekdays_of(times: np.ndarray) -> np.ndarray:
    """Weekday per bar, from the server clock rendered as UTC.

    MT5 stamps a bar with the server's wall clock encoded as a UTC epoch, so
    reading it back as UTC recovers the SERVER's calendar day, which is the
    day the venue means. Converting to local time here would shift the label
    by the operator's timezone and silently relabel bars near midnight.
    """
    return np.array([datetime.fromtimestamp(int(t), tz=timezone.utc).weekday()
                     for t in times])


def make_day_rule(days: tuple[str, ...], direction: int):
    """Take `direction` on the named weekdays, flat otherwise.

    `simulate()` enters at the NEXT bar's open from a signal on bar i-1, so
    the signal is placed on the bar BEFORE the day to be traded. Getting this
    wrong by one bar would trade Wednesday's signal on Thursday and still
    look like a Thursday rule.
    """
    want = {DAYS[d] for d in days}

    def rule(market):
        wd = weekdays_of(market["time"])
        sig = np.zeros(len(wd), dtype=float)
        # Signal on bar i means a trade opened at bar i+1's open, so mark the
        # bar whose SUCCESSOR falls on a wanted day.
        target = np.isin(wd, list(want))
        sig[:-1] = np.where(target[1:], direction, 0.0)
        return sig

    rule.wants_market = True
    return rule


def build_calendar_candidates():
    """Ten candidates: the measured days, their inverses, and the controls."""
    out = []
    tested = [
        ("thu_fri", ("Thu", "Fri")),   # both measured-negative days
        ("fri", ("Fri",)),             # the strongest single day
        ("thu", ("Thu",)),
        ("mon", ("Mon",)),             # the weekend-return day
        ("tue", ("Tue",)),             # CONTROL: no measured effect (t 0.95)
        ("wed", ("Wed",)),             # CONTROL: no measured effect (t 1.41)
    ]
    for name, days in tested:
        out.append((f"cal_short_{name}", "calendar_short", make_day_rule(days, -1)))
        out.append((f"cal_long_{name}", "calendar_long", make_day_rule(days, +1)))
    return out


def _call_signal(fn, m):
    """Pass the whole market dict to a rule that asks for it.

    `rule_search._call_signal` hands a rule (o, h, l, c) or (o, h, l, c, v).
    A calendar rule needs the TIMESTAMPS, which neither shape carries, so
    rules built here set `wants_market` and receive the dict. Everything
    else -- the null, the thresholds, the era blocks, the walk-forward, the
    date clustering, the measured spread -- is untouched, which is the point:
    the new universe is judged by the machinery that rejected the other ten.
    """
    if getattr(fn, "wants_market", False):
        return fn(m)
    return _ORIGINAL_CALL(fn, m)


_ORIGINAL_CALL = rule_search._call_signal


def main():
    rule_search.build_candidates = build_calendar_candidates
    rule_search._call_signal = _call_signal
    return rule_search.main()


if __name__ == "__main__":
    raise SystemExit(main())
