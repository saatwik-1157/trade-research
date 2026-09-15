#!/usr/bin/env python
"""Harvest positions as soon as they show a floating profit, and keep trading.

DEMO ACCOUNTS ONLY - the same fence as mt5_paper, enforced by assert_demo.

What this does, stated plainly, because the win rate it produces is the most
misleading number this repository can generate:

Closing at the first sign of profit caps every winner at roughly the threshold
while leaving every loser at its full stop distance. That is the 3.0xATR /
0.5xATR bracket taken to its limit - a reward:risk of about 0.01 rather than
0.17 - and it manufactures a win rate above 90% by construction. The balance
rises for as long as no stop is hit and gives it back when one is. Expectancy
is the spread, paid on every round trip, and it is negative.

So the harvest count is not a result. The figure to read is the R-multiple in
`track_record.py`, which divides each outcome by the money that was actually at
risk: a harvested win is worth about +0.01R and a stop is -1.00R, and no number
of the former pays for one of the latter. Every order and close is written to
the same log mt5_paper uses, so the record accumulates whether or not anyone
looks at it.

Usage:
    python tools/take_profit.py --minutes 60 --live
    python tools/take_profit.py --min-profit 0.05 --minutes 30 --live
    python tools/take_profit.py --harvest-only --live      # no new entries
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import kill_switch
import mt5_paper
import risk_gate
from mt5_paper import RefuseToTrade


#: Consecutive failed passes before a session gives up. At the default 20s
#: interval this is about three minutes of a terminal not answering -- long
#: enough to ride out a reconnect, short enough not to spin all night.
MAX_CONSECUTIVE_MISSES = 10

#: Called once per pass with (passes, state) when a wrapper sets it, so a
#: session's last known state survives a kill. `run_overnight.py` points this
#: at `crash_report.beat`. Left None here because take_profit.py is also run
#: directly, and a heartbeat is the wrapper's concern rather than the loop's.
#: Failures are swallowed at the call site: a crash journal that can end a
#: session is worse than no crash journal.
PASS_HOOK = None

#: The last completed session's summary, including the post-flush
#: `still_open`. Read by run_overnight.py when it closes the crash record.
LAST_SUMMARY = None

#: Set by a console control handler to ask the loop to wind down at the next
#: opportunity. Checked once per pass. Polled rather than acted on from the
#: handler thread, because closing a position from a handler while the loop is
#: mid-order is how one intention becomes two.
STOP_REQUESTED = False


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def modern_standby() -> bool | None:
    """Whether this machine sleeps the S0 way. None when it cannot be told.

    It matters because `ES_SYSTEM_REQUIRED` does not hold an S0 machine awake.
    The call SUCCEEDS -- it returns a non-zero previous state, so the session
    prints "holding the machine awake" -- and the system enters low-power idle
    anyway. Measured 2026-09-15: two gaps of almost exactly three hours in a
    session that had reported the hold, on AC, with the AC idle timeout set to
    "never".

    `powercfg /a` is asked rather than the registry: `CsEnabled` is absent on
    this machine even though S0 is the only standby it has.
    """
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["powercfg", "/a"], capture_output=True, text=True,
                             timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return parse_standby(out.stdout)


def parse_standby(text: str) -> bool:
    """Whether `powercfg /a` output says S0 low power idle is AVAILABLE.

    Only the available half counts. S0 is named in the unavailable half too --
    as the REASON S1, S2 and S3 are disabled -- so a plain substring search
    over the whole output answers True on an S3-only machine as well, which is
    exactly backwards.
    """
    head, _, _ = text.partition("The following sleep states are not available")
    return "S0 Low Power Idle" in head


@contextlib.contextmanager
def keep_awake(need_the_deadline: bool = False):
    """Hold the machine awake for as long as the session runs.

    A session told to be flat by 06:00 cannot close anything while the laptop
    is asleep. Measured on this machine 2026-09-07: idle standby at 300 minutes
    on AC and 45 on battery, against a session that needed 514 -- so the
    deadline would have arrived with the process suspended, and the morning's
    log would have ended mid-evening with every position still open and no
    error anywhere to explain it.

    Scoped to the run and restored on the way out, which is why this is not
    `powercfg`: changing the machine's power plan for a trading loop leaves the
    machine changed after the loop, and a laptop that never sleeps again is a
    worse bug than the one being fixed.

    **The display is held too on an S0 machine, and that is not a stylistic
    choice.** Holding only the SYSTEM was correct for S3 and is useless under
    Modern Standby, where the transition follows the screen going off rather
    than an idle timer: `ES_SYSTEM_REQUIRED` alone returns success and the
    machine idles anyway. It happened twice on the night of 2026-09-14, and the
    cost was the whole point of the session -- S0 disconnects the network, so
    the 06:00 flush met `retcode=10031` six times, gave up with seven positions
    open, and five of them then stopped out unmanaged for -21.56.

    So on S0 the screen stays lit. That is worse to look at and better than a
    wind-down that does not run, and `need_the_deadline` keeps the cost where
    the benefit is: a session with no `--flat-by` holds the system only, as
    before. It still does not defeat closing the lid or an explicit sleep,
    neither of which is an idle timeout. Windows only; elsewhere it is a no-op
    and says so rather than pretending.
    """
    s0 = modern_standby() if need_the_deadline else False
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if s0 is not False and need_the_deadline:
        # None (undetermined) is treated as S0. A lit screen is recoverable;
        # a flush that never runs is not.
        flags |= ES_DISPLAY_REQUIRED

    held = 0
    try:
        held = ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        held = 0
    if not held:
        print("  note: could not hold the machine awake - if it sleeps before the "
              "deadline, nothing closes and the session simply stops")
    try:
        yield (bool(held), s0, bool(flags & ES_DISPLAY_REQUIRED))
    finally:
        if held:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                pass


def net_floating(position) -> float:
    """Floating P&L including carry.

    `profit` excludes swap, and a position held overnight can read positive on
    `profit` while being negative once financing is charged. Harvesting on the
    gross figure would book those as wins.
    """
    return float(position.profit) + float(getattr(position, "swap", 0.0) or 0.0)


def harvest(mt5, min_profit: float, live: bool) -> list[dict]:
    """Close every own position whose net floating P&L has reached min_profit."""
    return mt5_paper.close_own(mt5, live, where=lambda p: net_floating(p) >= min_profit)


#: A flush gets more than one attempt. The terminal can be awake and still have
#: no trade server behind it: a machine resuming from sleep answers
#: `positions_get` while `order_send` returns 10031, no connection, for the
#: seconds it takes the session to come back. Measured on 2026-09-08 -- one
#: attempt at 06:36:18, retcode 10031, and the position was still open that
#: evening carrying swap, because nothing tried again and nothing said why.
FLUSH_ATTEMPTS = 6
FLUSH_WAIT_SECONDS = 10.0

#: TRADE_RETCODE_MARKET_CLOSED. The one refusal that retrying cannot fix.
#:
#: 10031 (no connection) is why the retry loop exists: a machine resuming from
#: sleep answers `positions_get` while `order_send` refuses, and ten seconds
#: later it works. 10018 is the opposite -- the venue is not open and will not
#: be for hours or days, so six attempts over a minute is a minute spent
#: proving what the first answer already said.
#:
#: Measured 2026-09-12: a Friday-night session reached its 06:00 deadline after
#: the FX week had closed, and printed 42 identical refusals across six rounds.
MARKET_CLOSED = 10018


def flush_until_flat(mt5, live: bool, attempts: int = FLUSH_ATTEMPTS,
                     wait: float = FLUSH_WAIT_SECONDS) -> tuple[int, list[dict]]:
    """Close every own position, retrying while any refuse, and SAY which.

    Two failures this replaces, both from the same night. The flush counted
    only the closes that worked and printed `closed 0` for the ones that did
    not, so a refusal read exactly like an account that was already flat --
    the retcode was in the record and never reached the operator. And it made
    exactly one attempt, at the worst possible moment: the deadline fires the
    instant the machine wakes, which is when the trade server is least likely
    to be there.

    Returns (closed, still_failing). A dry run reports every position as gone
    on the first pass, so it never loops.
    """
    closed = 0
    failures: list[dict] = []
    for attempt in range(1, attempts + 1):
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            results = flatten(mt5, live)
        except Exception as exc:  # noqa: BLE001 - the one failure that must SHOUT
            print(f"  [{stamp}] THE FLUSH RAISED: {type(exc).__name__}: {exc}",
                  flush=True)
            results = []
        gone = [r for r in results if r.get("status") in ("CLOSED", "DRY_RUN")]
        closed += len(gone)
        for r in gone:
            print(f"  [{stamp}] FLAT    {r.get('symbol', '?'):<8} #{r['ticket']}")
        failures = [r for r in results
                    if r.get("status") not in ("CLOSED", "DRY_RUN")]
        # Every refusal is "the market is shut". Say so once and stop, rather
        # than proving it five more times -- and say what it means, because
        # "still open" after a flush reads like a bug when it is a calendar.
        market_closed = bool(failures) and all(
            r.get("retcode") == MARKET_CLOSED for r in failures)
        for r in failures:
            # The retcode is the whole point. 10031 is "no connection with the
            # trade server" and means try again; 10018 is a closed market and
            # means the session cannot be flat until it opens. Printing the
            # number rather than a guess at what it means keeps the operator
            # able to look it up.
            print(f"  [{stamp}] FLUSH REFUSED {r.get('symbol', '?'):<8} "
                  f"#{r.get('ticket')} status={r.get('status')} "
                  f"retcode={r.get('retcode')}", flush=True)
        if not failures:
            return closed, []
        if market_closed:
            print(f"  [{stamp}] THE MARKET IS CLOSED (retcode {MARKET_CLOSED} on "
                  f"all {len(failures)}). Retrying cannot change that, so the "
                  f"flush stops here.", flush=True)
            print(f"  [{stamp}] {len(failures)} position(s) stay open until the "
                  "venue reopens. They carry financing and the opening gap.",
                  flush=True)
            return closed, failures
        if attempt < attempts:
            print(f"  [{stamp}] {len(failures)} still open; retrying in "
                  f"{wait:.0f}s (attempt {attempt}/{attempts})", flush=True)
            time.sleep(wait)
    return closed, failures


def flatten(mt5, live: bool) -> list[dict]:
    """Close every own position, whatever it is worth.

    This BOOKS LOSSES. It is the deliberate end of `--flat-by`: a position that
    never came good is closed at what it is worth, because the instruction is
    to be flat by a time and not to be flat only if that is free. Nothing else
    in this file closes a losing position -- `harvest` has a floor and always
    has -- so this is the one call that can realise a loss on purpose, and it
    is why it is a separate function with its own name.
    """
    return mt5_paper.close_own(mt5, live, where=lambda p: True)


def threshold_at(args, remaining: float) -> float:
    """The profit floor to harvest at, `remaining` seconds before flat-by.

    Constant at `--min-profit` until the last `--relax-over` minutes, then
    decaying linearly to zero. The point is to close each position at the best
    moment it is offered rather than dumping all of them at the deadline: a
    floor that never moves means a position 40 cents up at 05:59 gets flushed
    at whatever it is worth at 06:00 instead.

    It decays to 0 and NOT below. Zero is break-even; going negative would be
    this function deciding how much loss is acceptable, and that is `flat_by`'s
    decision to make once, not a slope's to make continuously.
    """
    window = args.relax_over * 60.0
    if window <= 0 or remaining >= window:
        return args.min_profit
    if remaining <= 0:
        return 0.0
    return args.min_profit * (remaining / window)


def seconds_until(hhmm: str) -> float:
    """Seconds to the next local occurrence of `HH:MM`.

    Local, matching `run_overnight.minutes_until` -- the operator said 6am and
    meant the clock on the wall. NOT `server_now`: the broker's clock is right
    for bounding a history query and wrong for a human deadline, which is the
    distinction CLAUDE.md records about `server_day_start`.
    """
    hour, _, minute = hhmm.partition(":")
    now = datetime.now()
    target = now.replace(hour=int(hour), minute=int(minute or 0),
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run(mt5, args) -> dict:
    deadline = time.monotonic() + args.minutes * 60
    # The wind-down clock. `--flat-by` is a wall-clock hour and `--minutes` is
    # a duration, so they are resolved against each other here rather than
    # assumed equal: a session told to stop at 06:00 and be flat by 05:45 is a
    # sensible thing to ask for, and so is one where they coincide.
    flat_at = deadline if args.flat_by is None else (
        time.monotonic() + seconds_until(args.flat_by))
    harvested, opened, passes, flushed = 0, 0, 0, 0
    halted = False
    blind = 0  # consecutive passes whose venue reads did not answer
    # Last values the venue actually confirmed. None means "not read this
    # pass", never zero -- the heartbeat has to be able to say UNKNOWN, since
    # a record claiming 0 positions open would send a recovery run away empty.
    open_count: int | None = None
    balance_seen: float | None = None
    equity_seen: float | None = None
    # WHICH halt, not just that there was one. A risk halt and a dead
    # terminal call for opposite responses from anything supervising this
    # process: one must not be restarted, the other exists to be. Reported
    # as a value rather than left to be matched out of the printed line.
    halt_kind: str | None = None
    # Consecutive failed passes. A terminal that blinks once should not end a
    # session that has hours left to run; one that is genuinely gone should not
    # be retried all night.
    misses = 0
    # The third place `account_info()` was dereferenced without checking it.
    # A terminal that is already down when the session starts produced a raw
    # AttributeError here -- a stack trace instead of a reason, before a
    # single pass had run. Refuse to start rather than start blind: every
    # limit below is measured against this opening balance.
    opening = mt5.account_info()
    if opening is None:
        raise mt5_paper.VenueUnreadable(
            "account_info() returned None at startup; the terminal is not "
            "answering, so the session has no balance to measure against"
        )
    start_balance = opening.balance

    # A suspended session leaves no error, only a hole between two pass lines.
    # On the night of 2026-09-14 there were two of nearly three hours each and
    # nothing in the log named them -- they had to be found by reading
    # timestamps by hand, after the damage. The venue also disconnects across
    # an S0 standby, so the gap explains the `retcode=10031` that follows it.
    # Naming it costs one comparison a pass.
    last_pass_at = time.monotonic()

    while time.monotonic() < deadline:
        passes += 1
        stamp = datetime.now().strftime("%H:%M:%S")

        now_mono = time.monotonic()
        overslept = now_mono - last_pass_at
        if overslept > max(3 * args.interval, args.interval + 60):
            print(f"  [{stamp}] THE MACHINE SLEPT: {overslept / 60:.0f} minutes passed "
                  f"between passes, not {args.interval}s. Nothing was managed in that "
                  f"window and the venue connection may need to re-establish",
                  flush=True)
        last_pass_at = now_mono

        remaining = flat_at - time.monotonic()

        if args.flat_by is not None and remaining <= 0:
            # The deadline. Everything still open is closed at what it is
            # worth, and the count is reported separately from `harvested` --
            # a position that was flushed did not reach its target, and
            # pooling the two would hide exactly that.
            left = flatten(mt5, args.live)
            gone = [r for r in left if r.get("status") in ("CLOSED", "DRY_RUN")]
            flushed += len(gone)
            for r in gone:
                print(f"  [{stamp}] FLAT    {r.get('symbol', '?'):<8} #{r['ticket']}")
            still = len(mt5_paper.own_positions(mt5))
            print(f"  [{stamp}] flat-by reached: closed {len(gone)}, {still} still open",
                  flush=True)
            if not still:
                break
            time.sleep(args.interval)
            continue

        # ONE PASS MUST NOT BE ABLE TO END THE SESSION.
        #
        # There was no exception handling here at all, and the flush that makes
        # the account flat runs AFTER this loop -- so a single transient IPC
        # error at 02:00 killed the process and left every position open at
        # 06:00. The instruction was "be flat by six"; an unguarded `except`-less
        # loop quietly converted that into "be flat by six unless anything at
        # all goes wrong overnight".
        try:
            got = harvest(mt5, threshold_at(args, remaining), args.live)
        except Exception as exc:  # noqa: BLE001 - reported, counted, retried
            misses += 1
            print(f"  [{stamp}] PASS FAILED ({misses}/{MAX_CONSECUTIVE_MISSES}): "
                  f"{type(exc).__name__}: {exc}", flush=True)
            if misses >= MAX_CONSECUTIVE_MISSES:
                print(f"  [{stamp}] giving up after {misses} consecutive failures; "
                      "the terminal is not answering and a flush would fail too",
                      flush=True)
                halted = True
                halt_kind = "terminal"
                break
            time.sleep(args.interval)
            continue
        misses = 0

        closed = [r for r in got if r.get("status") in ("CLOSED", "DRY_RUN")]
        harvested += len(closed)
        for r in closed:
            print(f"  [{stamp}] HARVEST {r.get('symbol', '?'):<8} #{r['ticket']}")

        # Nothing new inside the wind-down window. Opening a trade that the
        # flush will close minutes later pays the spread for no observation.
        winding_down = args.flat_by is not None and remaining <= args.relax_over * 60.0
        # Why a pass opened nothing. `cycle` already decides this per symbol and
        # returns it -- no_signal, no_history, skipped_already_open,
        # skipped_max_positions -- and none of it was ever printed, so a session
        # that opened twice in 1201 passes looked identical whether the rule was
        # being selective or the terminal was returning no bars. With
        # rsi_reversion, which signals rarely by design, that is the difference
        # between working and broken, and it was not observable.
        why = ""
        if not args.harvest_only and not winding_down:
            try:
                res = mt5_paper.cycle(mt5, args)
            except mt5_paper.VenueUnreadable as exc:
                # NOT an entry failure. cycle() computes the daily-loss limit
                # and the position cap from reads that did not answer, so
                # continuing would run both limits against no data. Counted
                # toward the same budget as a failed harvest so the tested
                # give-up path below fires.
                blind += 1
                print(f"  [{stamp}] VENUE UNREADABLE ({blind}/{MAX_CONSECUTIVE_MISSES}): "
                      f"{exc}", flush=True)
                if blind >= MAX_CONSECUTIVE_MISSES:
                    print(f"  [{stamp}] giving up after {blind} consecutive unreadable "
                          "passes; the terminal is not answering and a flush would "
                          "fail too", flush=True)
                    halted = True
                    halt_kind = "terminal"
                    break
                time.sleep(args.interval)
                continue
            except Exception as exc:  # noqa: BLE001 - an entry that failed is
                # not a reason to stop MANAGING what is already open. Skip the
                # entry, keep harvesting, keep the deadline.
                print(f"  [{stamp}] ENTRY FAILED: {type(exc).__name__}: {exc}",
                      flush=True)
                res = {"halted": False, "actions": []}
            if res["halted"]:
                print(f"  [{stamp}] HALTED: {res['reason']}")
                halted = True
                halt_kind = "risk"
                break
            sent = [a for a in res["actions"] if a.get("status") in ("SENT", "DRY_RUN")]
            opened += len(sent)
            if not sent:
                tally: dict[str, int] = {}
                for a in res["actions"]:
                    key = str(a.get("status", "?"))
                    tally[key] = tally.get(key, 0) + 1
                why = "  why=" + ",".join(f"{k}:{v}" for k, v in sorted(tally.items()))
            for a in sent:
                print(f"  [{stamp}] OPEN    {a['symbol']:<8} {a['side']:<5} @ {a.get('price')}")

        bal = mt5.account_info()
        if bal is None:
            # `account_info()` returns None when the terminal drops, and the
            # old handler here caught the resulting AttributeError as though a
            # log line had failed to format -- "the session continues".
            # Observed 2026-09-10: 23 consecutive passes, harvesting nothing,
            # opening nothing, halting nothing, with --max-daily-loss unable
            # to evaluate. A disconnect is not a cosmetic failure.
            blind += 1
            open_count = balance_seen = equity_seen = None
            print(f"  [{stamp}] pass {passes}: ACCOUNT UNREADABLE "
                  f"({blind}/{MAX_CONSECUTIVE_MISSES}); account_info() returned None",
                  flush=True)
            if blind >= MAX_CONSECUTIVE_MISSES:
                print(f"  [{stamp}] giving up after {blind} consecutive unreadable "
                      "passes; the terminal is not answering and a flush would fail too",
                      flush=True)
                halted = True
                halt_kind = "terminal"
                break
        else:
            try:
                open_count = len(mt5_paper.own_positions(mt5))
                balance_seen, equity_seen = float(bal.balance), float(bal.equity)
                print(f"  [{stamp}] pass {passes}: harvested={harvested} opened={opened} "
                      f"balance={bal.balance:,.2f} equity={bal.equity:,.2f} "
                      f"open={open_count}{why}", flush=True)
                blind = 0
            except mt5_paper.VenueUnreadable as exc:
                blind += 1
                print(f"  [{stamp}] pass {passes}: ACCOUNT UNREADABLE "
                      f"({blind}/{MAX_CONSECUTIVE_MISSES}); {exc}", flush=True)
                if blind >= MAX_CONSECUTIVE_MISSES:
                    print(f"  [{stamp}] giving up after {blind} consecutive unreadable "
                          "passes; the terminal is not answering and a flush would "
                          "fail too", flush=True)
                    halted = True
                    halt_kind = "terminal"
                    break
            except Exception as exc:  # noqa: BLE001 - a formatting fault is not a session
                print(f"  [{stamp}] pass {passes}: could not format the status line "
                      f"({type(exc).__name__}); the session continues", flush=True)

        if PASS_HOOK is not None:
            try:
                PASS_HOOK(passes, {
                    "harvested": harvested,
                    "opened": opened,
                    "positions_open": open_count,
                    "balance": balance_seen,
                    "equity": equity_seen,
                    "blind": blind,
                    "misses": misses,
                })
            except Exception:  # noqa: BLE001 - a journal never ends a session
                pass

        # The operator's stop. Checked every pass rather than once at start,
        # because the point is to reach a session that is ALREADY running --
        # and, when detached, one with no console to signal. Breaking here
        # runs the flush below; killing the process would not.
        if kill_switch.engaged():
            print(f"  [{stamp}] KILL SWITCH: {kill_switch.reason()}", flush=True)
            print(f"  [{stamp}] winding down; the flush below still runs",
                  flush=True)
            halt_kind = "kill_switch"
            break

        if STOP_REQUESTED:
            # A console close or Ctrl-C. Break to the flush below rather than
            # exiting here: the whole point is that the wind-down still runs.
            print(f"  [{stamp}] stop requested; winding down", flush=True)
            halt_kind = "stopped"
            break

        if time.monotonic() + args.interval >= deadline:
            break
        time.sleep(args.interval)

    # THE FLUSH THAT ACTUALLY FIRES.
    #
    # The in-loop branch above only runs on a pass that starts after `flat_at`,
    # and when --flat-by is the same hour the session stops at there is no such
    # pass: the loop breaks one interval BEFORE the deadline. So a wind-down
    # configured the obvious way -- stop at 06:00, be flat by 06:00 -- would
    # have closed nothing and reported success. Caught before it ran, by asking
    # which pass performs the close rather than by reading the flag.
    #
    # Not after a HALT. `--max-daily-loss` firing at 22:00 is a reason to stop
    # trading, not a reason to close every position hours before the operator
    # asked; the deadline is the deadline.
    if args.flat_by is not None and not halted:
        stamp = datetime.now().strftime("%H:%M:%S")
        gone_count, still = flush_until_flat(mt5, args.live)
        flushed += gone_count
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"  [{stamp}] flat-by {args.flat_by}: closed {gone_count} at the deadline",
              flush=True)
        if still:
            print(f"  [{stamp}] POSITIONS ARE STILL OPEN AT THE VENUE after "
                  f"{FLUSH_ATTEMPTS} attempts: "
                  + ", ".join(f"{r.get('symbol', '?')} retcode={r.get('retcode')}"
                              for r in still))
            print(f"  [{stamp}] Close them by hand, or start a wind-down session: "
                  "python tools/run_overnight.py --harvest-only --relax-over 0",
                  flush=True)

    # The SUMMARY must not be able to kill the session either. This block read
    # the terminal twice, unguarded, so a session that gave up cleanly on a dead
    # terminal still died with a traceback on its way out and reported nothing
    # about what it had done. Found by the test written for the loop guard,
    # which is the argument for writing the test.
    # `flat_by` is recorded because "7 positions are open" means two different
    # things depending on whether being flat was ever asked for, and the
    # session record could not tell them apart. A run with no --flat-by that
    # ends holding 7 is working as instructed; one WITH it has failed its last
    # obligation, and only this field separates them.
    summary = {"passes": passes, "harvested": harvested, "opened": opened,
               "flushed": flushed, "halted": halted, "halt_kind": halt_kind,
               "flat_by": args.flat_by,
               "start_balance": start_balance,
               "end_balance": None, "realised": None, "still_open": None}
    try:
        end = mt5.account_info()
        summary["end_balance"] = end.balance
        summary["realised"] = round(end.balance - start_balance, 2)
    except Exception as exc:  # noqa: BLE001
        print(f"  could not read the closing balance ({type(exc).__name__})")
    try:
        summary["still_open"] = len(mt5_paper.own_positions(mt5))
    except Exception as exc:  # noqa: BLE001
        # NOT zero. "We could not ask" and "nothing is open" are the distinction
        # this whole repository is built on.
        print(f"  could not count open positions ({type(exc).__name__}); "
              "treat the account as UNKNOWN, not flat")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-profit", type=float, default=0.01,
                    help="close a position once net floating P&L reaches this (account currency)")
    ap.add_argument("--minutes", type=float, default=30.0, help="how long to run")
    ap.add_argument("--interval", type=int, default=20, help="seconds between passes")
    ap.add_argument("--harvest-only", action="store_true",
                    help="close winners but open nothing new")
    ap.add_argument("--flat-by", default=None, metavar="HH:MM",
                    help="local time to hold NO position past. Everything still open "
                         "then is closed at what it is worth, losses included; nothing "
                         "new is opened inside the --relax-over window before it")
    ap.add_argument("--relax-over", type=float, default=0.0, metavar="MINUTES",
                    help="decay --min-profit linearly to zero over the final MINUTES "
                         "before --flat-by, so positions close at the best moment "
                         "offered rather than all at the deadline")
    ap.add_argument("--rule", choices=sorted(mt5_paper.RULES), default="random")
    ap.add_argument("--symbols", default="EURUSD,GBPUSD,USDJPY,USDCAD,AUDUSD,USDCHF,NZDUSD")
    ap.add_argument("--lot", type=float, default=0.01)
    ap.add_argument("--risk-usd", type=float, default=None,
                    help="size each trade so its stop costs this much; "
                         "overrides --lot. Does not change expectancy.")
    ap.add_argument("--sl-atr", type=float, default=1.5)
    ap.add_argument("--tp-atr", type=float, default=1.5)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--max-daily-loss", type=float, default=50.0)
    ap.add_argument("--max-risk-per-trade", type=float, default=None,
                    metavar="USD",
                    help="refuse an entry whose stop would cost more than this. "
                         "Off by default, and deliberately NOT --risk-usd: the "
                         "same number on both sides is a check that cannot "
                         "fail. It catches the min-lot floor, where the volume "
                         "step forces more risk than the budget asked for")
    ap.add_argument("--live", action="store_true", help="actually send orders (demo only)")
    ap.add_argument("--max-consecutive-losses", type=int, default=0,
                    metavar="N",
                    help="pause new entries after N losing trades in a row "
                         "(0 = off); open positions are still managed")
    ap.add_argument("--path", default=None)
    args = ap.parse_args()
    args.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    try:
        mt5 = mt5_paper.connect(args.path)
        acct = mt5_paper.assert_demo(mt5, live=args.live)
    except RefuseToTrade as exc:
        print(f"\n  REFUSED: {exc}\n")
        return 1

    # The gate reads its account and mode from here. `cycle()` builds it once
    # per pass from `args`, so this is the whole of the wiring.
    args.account = acct

    print(f"\n  account {acct['login']} @ {acct['server']}  [{acct['mode']}]  "
          f"balance {acct['balance']:,.2f} {acct['currency']}")
    print(f"  harvest at >= {args.min_profit} {acct['currency']}  "
          f"entries={'off' if args.harvest_only else args.rule}  "
          f"sl={args.sl_atr}xATR tp={args.tp_atr}xATR")
    sizing = (
        "risk %.2f %s per trade off the stop distance" % (args.risk_usd, acct["currency"])
        if args.risk_usd
        else "fixed %g lots" % args.lot
    )
    print(f"  size: {sizing}")
    print(f"  mode: {'LIVE ORDERS (demo account)' if args.live else 'DRY RUN - no orders sent'}")

    # Say whether the fence is up, at the top, before anything is sent. An
    # engine that could not be imported refuses every opening order, and an
    # operator should learn that from the banner rather than from 200 refusals.
    if risk_gate.ENGINE_IMPORT_ERROR is None:
        print("  risk: RiskEngine rules on every entry; closes are recorded, never refused")
    else:
        print(f"  risk: THE ENGINE COULD NOT BE LOADED -- {risk_gate.ENGINE_IMPORT_ERROR}")
        print("        no entry will be sent. Closes and the flush still run.")

    print(f"  running {args.minutes:g} minutes, one pass every {args.interval}s")
    if args.flat_by:
        mins = seconds_until(args.flat_by) / 60.0
        print(f"  FLAT BY {args.flat_by} local, in {mins:.0f} minutes -- everything "
              f"still open then is closed at what it is worth, LOSSES INCLUDED")
        if args.relax_over:
            print(f"  the {args.min_profit} floor decays to 0 over the final "
                  f"{args.relax_over:g} minutes, and nothing new opens inside it")
    print()

    try:
        with keep_awake(need_the_deadline=bool(args.flat_by)) as (awake, s0, screen):
            if awake and args.flat_by:
                if screen:
                    reason = ("this machine only has S0 standby" if s0
                              else "the standby type could not be determined")
                    print(f"  holding the machine AND THE SCREEN awake until the session "
                          f"ends -- {reason}, and holding the system alone does not "
                          f"work there\n")
                else:
                    print("  holding the machine awake until the session ends "
                          "(the display may still sleep)\n")
            out = run(mt5, args)
            # The heartbeat's last state is the last PASS, taken before the
            # flush. A completed session whose record still says 7 open is
            # the confusion the journal exists to prevent, so the wrapper
            # reads the post-flush summary from here.
            global LAST_SUMMARY
            LAST_SUMMARY = out
    finally:
        mt5.shutdown()

    print(f"\n  passes {out['passes']}   harvested {out['harvested']}   "
          f"opened {out['opened']}   flushed {out['flushed']}   "
          f"still open {'UNKNOWN' if out['still_open'] is None else out['still_open']}")

    if args.flat_by and out["still_open"] is None:
        # UNKNOWN is the LOUDEST case, not the quietest. `if out["still_open"]`
        # treated None as falsy and skipped this warning exactly when nobody
        # knew whether the account was flat -- the same "missing is never safe"
        # rule `portfolio.decision` states, broken in a print statement.
        print(f"  WARNING: --flat-by {args.flat_by} finished and the account could "
              "NOT be read. Whether anything is still open is UNKNOWN; check the "
              "terminal before assuming it is flat")
    elif out["still_open"] and args.flat_by:
        # Said plainly rather than left to be read off a count. A wind-down
        # that did not finish is the one outcome of this mode that matters.
        print(f"  WARNING: --flat-by {args.flat_by} did not leave the account flat; "
              f"{out['still_open']} position(s) are still open at the venue")

    if out["end_balance"] is None:
        print(f"  balance {out['start_balance']:,.2f} -> UNKNOWN "
              "(the closing balance could not be read)")
    else:
        print(f"  balance {out['start_balance']:,.2f} -> {out['end_balance']:,.2f}  "
              f"({out['realised']:+.2f})")
    print("\n  A harvest count is not a win rate and this balance is not an edge.")
    print("  Read the R-multiple: python tools/track_record.py --merge\n")

    # A halt is not a clean finish, and the two halts are not each other.
    # 2 is the risk limit: whatever supervises this must NOT start another
    # session, because restarting through a daily loss limit is how a limit
    # becomes a speed bump. 3 is a terminal that stopped answering, which is
    # exactly what a supervisor is for. This DOES change the dated session:
    # a run that hit its daily loss limit used to exit 0 like any other, and
    # now exits 2. That is the point -- 'the session ended' and 'the session
    # was stopped by a risk limit' were the same answer, and the launcher
    # printed the same line for both.
    if out["halt_kind"] == "risk":
        return 2
    if out["halt_kind"] == "terminal":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
