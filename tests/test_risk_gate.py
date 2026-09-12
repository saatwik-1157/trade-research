#!/usr/bin/env python
"""The P2 fence: no opening order reaches a venue without a risk decision.

The audit's first critical finding was that this repository had two paths to a
broker and the safety machinery was on the one that had never traded. The
harness opened 21 positions on the night of 2026-09-10 through four
`order_send` sites that imported nothing from `app/`.

The properties here are about what CANNOT happen, so most of them are asserted
against `FakeMT5.sent` -- the list of requests that actually reached the venue.
A refusal that still sends the order is not a refusal.

    * a LIVE opening order with no risk decision behind it is not sent,
    * an engine that could not be loaded refuses rather than waves through,
    * ...and the mirror of that: a CLOSE is never refused, by anything, ever,
      because the `--flat-by` flush runs through the same code and the flush
      is the one mechanism bounding a losing tail overnight,
    * the platform path is not double-judged,
    * and a HALT ends the pass rather than collecting six identical refusals.

Runs on 3.10, 3.12 and 3.14. On 3.10 `app/risk/engine.py` genuinely cannot be
imported -- it uses `StrEnum` and `datetime.UTC`, both 3.11 -- and that is not
a reason to skip the file: the fail-closed behaviour is exactly what has to be
proven on the interpreter that cannot load the engine. The checks that need a
live engine announce themselves as unexercised there rather than passing
quietly.
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import mt5_paper  # noqa: E402
import risk_gate  # noqa: E402

FAILED = []
UNEXERCISED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:58s} got={got} want={want}")
    if not ok:
        FAILED.append(label)


def needs_engine(label):
    """Say out loud that a property was not tested here. A skip that prints
    nothing is how an untested interpreter comes to look like a tested one."""
    print(f"  ----  {label:58s} NOT EXERCISED: {risk_gate.ENGINE_IMPORT_ERROR}")
    UNEXERCISED.append(label)


class FakeMT5:
    """Enough of the MT5 surface for place() and close_own().

    `sent` is the point of it: every request that reached the venue, in order.
    """

    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    TRADE_RETCODE_DONE = 10009
    TIMEFRAME_H1 = 16385
    DEAL_ENTRY_OUT = 1

    def __init__(self, bid=1.16000, positions=()):
        self.bid = bid
        self.sent = []
        self._positions = positions

    def symbol_info(self, symbol):
        return types.SimpleNamespace(
            digits=5, point=0.00001, filling_mode=1,
            trade_tick_value=1.0, trade_tick_size=0.00001,
            volume_step=0.01, volume_min=0.01, volume_max=100.0)

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info_tick(self, symbol):
        return types.SimpleNamespace(ask=self.bid + 0.00002, bid=self.bid, time=0)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        import numpy as np

        n = 300
        return np.rec.fromarrays(
            [np.full(n, self.bid + 0.00038),
             np.full(n, self.bid - 0.00038),
             np.full(n, self.bid)],
            names="high,low,close")

    def positions_get(self):
        return self._positions

    def history_deals_get(self, *a, **k):
        return ()

    def order_send(self, request):
        self.sent.append(request)
        return types.SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE, order=990001, deal=880001,
            price=self.bid, volume=request.get("volume", 0.0))


class NoLog:
    """Keep the tests out of data/paper_trades.jsonl, and keep what they wrote."""

    def __enter__(self):
        self.records = []
        self._real = mt5_paper._log
        mt5_paper._log = self.records.append
        return self

    def __exit__(self, *exc):
        mt5_paper._log = self._real
        return False


class _Stand_in:
    """Stands in for an engine type that did not import.

    Accepts anything and does nothing, so a test can REACH the code it is
    about rather than dying on the first constructor it meets.
    """

    def __init__(self, *args, **kwargs):
        pass


class EngineAs:
    """Run a block with `risk_gate.RiskEngine` replaced.

    The unavailable path has to be testable on the interpreters where the
    engine DOES import, or it would only ever be exercised by the one CI job
    that cannot exercise anything else.

    The collaborators are stood in for as well, and that is not tidiness. On
    3.10 the engine does not import at all, so `RiskLimits`, `KillSwitches`,
    `OrderProposal` and `PortfolioState` are all None -- and `risk_gate`
    constructs them as ARGUMENTS to `RiskEngine(...)`, which Python evaluates
    first. So the first of them raised `TypeError: 'NoneType' object is not
    callable` before the replacement was ever called, and the record said that
    instead of what the replacement raised. Patching only `RiskEngine` tested
    nothing on the one interpreter this file exists to cover.

    Only None values are replaced. Where the engine really did import, the
    real types are used, so the test still runs against the real thing.
    """

    COLLABORATORS = ("RiskLimits", "KillSwitches", "OrderProposal", "PortfolioState")

    def __init__(self, replacement):
        self.replacement = replacement

    def __enter__(self):
        self._real = {"RiskEngine": risk_gate.RiskEngine}
        risk_gate.RiskEngine = self.replacement
        for name in self.COLLABORATORS:
            current = getattr(risk_gate, name, None)
            self._real[name] = current
            if current is None:
                setattr(risk_gate, name, _Stand_in)
        return self

    def __exit__(self, *exc):
        for name, value in self._real.items():
            setattr(risk_gate, name, value)
        return False


def _gate(**kw):
    kw.setdefault("account_id", "5055473926")
    kw.setdefault("mode", "DEMO")
    return risk_gate.Gate(**kw)


# --------------------------------------------------------------- the fence

def test_a_live_order_needs_a_decision():
    print()
    print("A live entry with no risk decision behind it is not sent")

    fake = FakeMT5()
    with NoLog() as log:
        out = mt5_paper.place(fake, "EURUSD", "buy", 0.01, 1.5, 1.5, live=True)

    check("the order is refused", out["status"], "RISK_GATE_MISSING")
    check("and NOTHING reached the venue", len(fake.sent), 0)
    check("the refusal is logged, not silent", len(log.records), 1)
    check("and the log says it was an order", log.records[0]["event"], "order")

    # The same call without --live was never the finding, and still is not.
    fake2 = FakeMT5()
    with NoLog():
        dry = mt5_paper.place(fake2, "EURUSD", "buy", 0.01, 1.5, 1.5, live=False)
    check("a dry run is unaffected", dry["status"], "DRY_RUN")
    check("and a dry run sends nothing either", len(fake2.sent), 0)


def test_the_platform_path_is_not_judged_twice():
    print()
    print("Path B arrives holding an Approval the OMS already required")

    fake = FakeMT5()
    with NoLog():
        out = mt5_paper.place(fake, "EURUSD", "buy", 0.01, 0.0, 0.0, live=True,
                              gate=risk_gate.APPROVED_UPSTREAM)

    check("the order is sent", out["status"], "SENT")
    check("exactly one request reached the venue", len(fake.sent), 1)
    check("and the record names where the approval came from",
          out["risk"]["decision"], "approved upstream")

    # 0.0 multiples are the platform's "no bracket, I will set the levels".
    # A gate that required a stop here would refuse every platform order.
    check("an unbracketed platform order is still sent", fake.sent[0]["sl"], 0.0)


def test_an_unloadable_engine_refuses_an_entry():
    print()
    print("Unknown is not permission: no engine, no entry")

    with EngineAs(None):
        gate = _gate(max_daily_loss=200.0)
        decision = gate.open(symbol="EURUSD", side="buy", volume=0.01,
                             entry_price=1.16, stop_loss=1.15, take_profit=1.17)
        check("the gate reports itself unavailable", gate.available, False)
        check("and refuses", decision.approved, False)
        check("without halting the session", decision.halt, False)

        fake = FakeMT5()
        with NoLog():
            out = mt5_paper.place(fake, "EURUSD", "buy", 0.01, 1.5, 1.5,
                                  live=True, gate=gate)
        check("place() sends nothing", len(fake.sent), 0)
        check("and says why", out["status"], "RISK_VETO")
        check("the record names the missing engine",
              out["risk"]["engine"], "unavailable")


# ------------------------------------------------- and the mirror of it

def test_a_close_is_never_refused():
    print()
    print("A close is never blocked -- not by a limit, not by a broken engine")

    with EngineAs(None):
        d = risk_gate.record_close(symbol="EURUSD", side="sell", volume=0.01)
        check("no engine still approves the close", d.approved, True)
        check("and the record says the evaluation did not happen",
              d.record["engine"], "unavailable")

    class Raises:
        def __init__(self, *a, **k):
            raise RuntimeError("engine exploded")

    with EngineAs(Raises):
        d = risk_gate.record_close(symbol="EURUSD", side="sell", volume=0.01)
        check("an engine that RAISES still approves the close", d.approved, True)
        check("and the exception is recorded rather than swallowed",
              "engine exploded" in d.record.get("raised", ""), True)

    # A volume this module could not read is its own problem, and is still
    # never a reason to leave a position open.
    d = risk_gate.record_close(symbol="EURUSD", side="sell", volume=None)
    check("an unreadable volume still approves the close", d.approved, True)


def test_the_flush_path_still_closes():
    print()
    print("close_own() runs through the recorder and is not gated by it")

    position = types.SimpleNamespace(
        ticket=58399391616, symbol="EURUSD", volume=0.01, type=0,
        magic=mt5_paper.MAGIC, profit=0.5, swap=0.0)
    fake = FakeMT5(positions=(position,))

    with EngineAs(None), NoLog():
        out = mt5_paper.close_own(fake, live=True)

    check("the close was sent even with no engine", len(fake.sent), 1)
    check("and reported closed", out[0]["status"], "CLOSED")
    check("carrying the record of the decision that was not made",
          out[0]["risk"]["engine"], "unavailable")


# --------------------------------------------------------- the halt path

def test_a_halt_ends_the_pass():
    print()
    print("A halt stops proposing; it does not collect one refusal per symbol")

    args = types.SimpleNamespace(
        symbols=["EURUSD", "GBPUSD", "USDJPY"], max_daily_loss=200.0,
        max_positions=7, max_consecutive_losses=0, rule="random", lot=0.01,
        sl_atr=1.5, tp_atr=1.5, live=False, risk_usd=None)

    calls = []

    def halting_place(mt5, symbol, *a, **kw):
        calls.append(symbol)
        return {"symbol": symbol, "status": "RISK_HALT",
                "risk_reason": "max_daily_loss: realised -250, limit -200"}

    class Venue:
        TIMEFRAME_H1 = 16385

        def positions_get(self):
            return ()

        def history_deals_get(self, *a):
            return []

        def copy_rates_from_pos(self, *a):
            import numpy as np

            return np.rec.fromarrays(
                [np.full(300, 1.1), np.full(300, 1.0), np.full(300, 1.05)],
                names="high,low,close")

    real_place = mt5_paper.place
    real_start = mt5_paper.server_day_start
    real_end = mt5_paper.history_end
    real_rule = mt5_paper.RULES["random"]
    mt5_paper.place = halting_place
    mt5_paper.server_day_start = lambda _m: 0
    mt5_paper.history_end = lambda _m: 1
    # `random` is a coin flip with two None faces, so left alone this test
    # would propose an order two passes in three and pass by accident.
    mt5_paper.RULES["random"] = lambda _rates: "buy"
    try:
        res = mt5_paper.cycle(Venue(), args)
    finally:
        mt5_paper.place = real_place
        mt5_paper.server_day_start = real_start
        mt5_paper.history_end = real_end
        mt5_paper.RULES["random"] = real_rule

    check("the pass is halted", res["halted"], True)
    check("after ONE symbol, not three", len(calls), 1)
    check("and the reason names the engine",
          res["reason"].startswith("risk engine:"), True)


# ------------------------------------------------- what the engine decides

def test_the_engine_rules_on_the_order():
    print()
    print("The verdicts themselves, when there is an engine to give them")

    if not risk_gate.Gate(account_id="x").available:
        for label in ("a clean entry is approved",
                      "a breached daily loss HALTS",
                      "an unknown P&L refuses rather than assumes zero",
                      "a losing streak vetoes without halting",
                      "an entry with no stop is refused",
                      "a symbol already open is refused"):
            needs_engine(label)
        return

    gate = _gate(max_daily_loss=200.0, max_open_positions=7,
                 max_consecutive_losses=3)
    gate.observe(realised_today=-14.95, consecutive_losses=1)
    gate.open_positions, gate.open_symbols = 2, ("GBPUSD", "USDJPY")

    ok = gate.open(symbol="EURUSD", side="buy", volume=0.01, entry_price=1.16,
                   stop_loss=1.15, take_profit=1.17)
    check("a clean entry is approved", ok.approved, True)
    check("and carries a decision id", bool(ok.decision_id), True)
    check("with the unfed limits named, not counted as passed",
          "max_weekly_loss" in ok.not_enforced, True)

    gate.observe(realised_today=-250.0, consecutive_losses=0)
    breached = gate.open(symbol="EURUSD", side="buy", volume=0.01,
                         entry_price=1.16, stop_loss=1.15)
    check("a breached daily loss refuses", breached.approved, False)
    check("and HALTS the session", breached.halt, True)

    gate.observe(realised_today=None, consecutive_losses=None)
    blind = gate.open(symbol="EURUSD", side="buy", volume=0.01,
                      entry_price=1.16, stop_loss=1.15)
    check("an unknown P&L refuses rather than assumes zero",
          blind.approved, False)

    gate.observe(realised_today=-1.0, consecutive_losses=5)
    streak = gate.open(symbol="EURUSD", side="buy", volume=0.01,
                       entry_price=1.16, stop_loss=1.15)
    check("a losing streak vetoes", streak.approved, False)
    # It clears itself on the next win. Halting would stop the session
    # managing the positions the streak just produced.
    check("and does NOT halt", streak.halt, False)

    gate.observe(realised_today=-1.0, consecutive_losses=0)
    naked = gate.open(symbol="EURUSD", side="buy", volume=0.01,
                      entry_price=1.16, stop_loss=0.0)
    check("an entry with no stop is refused", naked.approved, False)
    check("naming the stop, not something else",
          "stop_loss_required" in naked.failed, True)

    gate.open_symbols = ("EURUSD",)
    clash = gate.open(symbol="EURUSD", side="buy", volume=0.01,
                      entry_price=1.16, stop_loss=1.15)
    check("a symbol already open is refused", clash.approved, False)


def test_the_gate_reads_the_session_flags():
    print()
    print("gate_for() configures only what the harness can feed")

    if not risk_gate.Gate(account_id="x").available:
        needs_engine("the flags reach the engine")
        return

    args = types.SimpleNamespace(max_daily_loss=200.0, max_positions=7,
                                 max_consecutive_losses=3,
                                 max_risk_per_trade=None)
    gate = risk_gate.gate_for(args, {"login": 5055473926, "mode": "DEMO"})
    check("the account is named", gate.account_id, "5055473926")
    check("and the mode is lowercased into the engine's check", gate.mode, "demo")

    # An unconfigured limit must be reported, never silently passed.
    gate.observe(realised_today=-1.0, consecutive_losses=0)
    gate.open_positions, gate.open_symbols = 0, ()
    d = gate.open(symbol="EURUSD", side="buy", volume=0.01, entry_price=1.16,
                  stop_loss=1.15)
    check("correlated exposure has no data source and says so",
          "max_correlated_exposure" in d.not_enforced, True)


def test_the_import_itself_fails_closed():
    print()
    print("What CI's Python 3.10 job actually does, run from here")

    # 3.10 has no `StrEnum` and no `datetime.UTC`, so `app/risk/engine.py`
    # raises on import there. Everything above tests the CONSEQUENCE of an
    # unavailable engine by replacing `RiskEngine`; this tests the import
    # itself, because a module that raised at import time would take the whole
    # harness down instead of refusing entries -- and it would do so only on
    # the interpreter nobody runs the harness on.
    import importlib

    class Blocker:
        def find_spec(self, name, path=None, target=None):
            if name == "app.risk.engine":
                raise ImportError("cannot import name 'StrEnum' from 'enum'")
            return None

    before = risk_gate.ENGINE_IMPORT_ERROR
    blocker = Blocker()
    sys.meta_path.insert(0, blocker)
    sys.modules.pop("app.risk.engine", None)
    try:
        importlib.reload(risk_gate)
        check("the module still imports", risk_gate.ENGINE_IMPORT_ERROR is not None, True)
        check("naming what refused it",
              "StrEnum" in (risk_gate.ENGINE_IMPORT_ERROR or ""), True)

        gate = risk_gate.Gate(account_id="5055473926")
        entry = gate.open(symbol="EURUSD", side="buy", volume=0.01,
                          entry_price=1.16, stop_loss=1.15)
        check("an entry is refused", entry.approved, False)
        closed = risk_gate.record_close(symbol="EURUSD", side="sell", volume=0.01)
        check("and a close is not", closed.approved, True)
    finally:
        sys.meta_path.remove(blocker)
        importlib.reload(risk_gate)

    # Back to whatever it was, which on 3.10 is a genuine import failure.
    check("and the module is left as it was found",
          risk_gate.ENGINE_IMPORT_ERROR, before)


def main():
    print("risk_gate / the P2 fence on Path A")
    test_a_live_order_needs_a_decision()
    test_the_platform_path_is_not_judged_twice()
    test_an_unloadable_engine_refuses_an_entry()
    test_a_close_is_never_refused()
    test_the_flush_path_still_closes()
    test_a_halt_ends_the_pass()
    test_the_engine_rules_on_the_order()
    test_the_gate_reads_the_session_flags()
    test_the_import_itself_fails_closed()

    print()
    if UNEXERCISED:
        print(f"{len(UNEXERCISED)} check(s) need an importable risk engine and "
              f"did not run on Python {sys.version.split()[0]}")
    if FAILED:
        print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
