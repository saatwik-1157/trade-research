#!/usr/bin/env python
"""Start the harvest session and stop it at a wall-clock hour.

DEMO ACCOUNTS ONLY - the fence is mt5_paper.assert_demo, reached through
take_profit.

This exists so the run can be started with one short command instead of a
twelve-argument line retyped at midnight, and so the settings below are
recorded in a file that can be read and argued with rather than in shell
history. It computes the minutes to the next occurrence of --until-hour on
the LOCAL clock and hands them to take_profit.

What the settings are, and why:

  rule=random        The rule that has a signal on every free symbol every
                     pass, which is what "close one and open another" needs.
                     It is also the honest choice: rule_search.py found no
                     entry rule that beats a permutation null out of sample,
                     so a named rule here would imply a selection that has
                     not been demonstrated.
  risk-usd 5         Constant risk per trade, sized off each symbol's stop
                     distance by mt5_paper.lot_for_risk. It equalises the
                     bet across symbols; it does not improve expectancy, and
                     cannot - volume is a positive multiplier on the result
                     and leaves the sign alone.
  min-profit 0.50    Harvest at 0.1R. Harvesting at a cent is what produced
                     the 92% win rate against a 0.17 payoff ratio already in
                     the ledger: a win rate manufactured by bracket geometry
                     is not a measurement of anything.
  max-daily-loss 200 About 40 stops at this risk. A fence, not a forecast.

The balance line this prints at the end is not a result. Read the
R-multiple and the date-clustered t:

    python tools/track_record.py --merge

Usage:
    python tools/run_overnight.py                  # run until 06:00, live
    python tools/run_overnight.py --until-hour 9   # run until 09:00
    python tools/run_overnight.py --dry-run        # show the command only
    python tools/run_overnight.py --paper          # no orders sent
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SETTINGS = [
    "--rule", "random",
    "--symbols", "EURUSD,GBPUSD,USDJPY,USDCAD,AUDUSD,USDCHF,NZDUSD",
    "--risk-usd", "5",
    "--sl-atr", "1.5",
    "--tp-atr", "1.5",
    "--min-profit", "0.50",
    "--max-positions", "7",
    "--max-daily-loss", "200",
    "--interval", "20",
]


def minutes_until(hour: int) -> tuple[float, datetime]:
    """Minutes from now to the next occurrence of `hour`:00 on the local clock.

    Rolls to tomorrow when the hour has already passed today, which is the
    normal case for an overnight run started in the evening.
    """
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds() / 60.0, target


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--until-hour", type=int, default=6,
                    help="local hour to stop at (default 6, i.e. 06:00)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved command and exit without connecting")
    ap.add_argument("--paper", action="store_true",
                    help="run without sending orders")
    args = ap.parse_args()

    if not 0 <= args.until_hour <= 23:
        print(f"  --until-hour must be 0-23, got {args.until_hour}")
        return 1

    minutes, target = minutes_until(args.until_hour)
    argv = SETTINGS + ["--minutes", f"{minutes:.0f}"]
    if not args.paper:
        argv.append("--live")

    print(f"\n  now {datetime.now():%H:%M} -> stop {target:%H:%M} "
          f"({minutes:.0f} minutes)")
    print("  take_profit.py " + " ".join(argv) + "\n")

    if args.dry_run:
        return 0

    import take_profit
    sys.argv = ["take_profit.py"] + argv
    return take_profit.main()


if __name__ == "__main__":
    raise SystemExit(main())
