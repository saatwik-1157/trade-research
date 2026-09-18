#!/usr/bin/env python
"""What an MT5 retcode means, in the only terms a caller acts on.

Seeded ONLY from codes this repository has actually observed. The MT5 manual
lists dozens more; a row for one of those would be a figure nobody here has
measured, which is the thing this project refuses to write down. When an
unseen code arrives it is classified `UNCLASSIFIED` and surfaced as its raw
integer rather than guessed at.

WHY THIS EXISTS
---------------
The same defect was fixed twice by hand on 2026-09-18 and would have been
fixed a third time next week:

* `tools/mt5_paper.py` recorded a raised IPC error and a `None` return as
  `REJECTED` -- a claim that the VENUE refused the order. It is the opposite
  claim, and it decides whether a retry is safe.
* `tools/take_profit.py` special-cased exactly one integer, `MARKET_CLOSED =
  10018`, and treated every other refusal identically.

One table, imported by both paths, so a code means the same thing wherever it
lands.

THE FOUR FIELDS, AND WHY IT IS NOT THREE
----------------------------------------
`transmitted` answers "did the request reach the server". The question the
callers actually ask is different: **can a resend open a SECOND position.**
For every code here the two answers differ -- six are transmitted-yes and
booked-no, which is the safe combination -- so collapsing them loses exactly
the distinction the module exists for.

`retry` is five-valued rather than a boolean, because four of the eight codes
mean "resending THIS request is futile, sending a corrected one is fine". A
bool licenses either a pointless ten-minute loop or a missed recovery.

THE EVIDENCE
------------
Counts verified over all 2,180 rows of `data/paper_trades.jsonl`:

    order/SENT/10009      1089      close/CLOSED/10009     875
    close/FAILED/10018      82      close/FAILED/10031      73
    order/RISK_VETO/None    36      order/REJECTED/10031    12
    order/REJECTED/10016    10      order/REJECTED/10030     1
    order/REJECTED/10017     1      close/FAILED/10036       1
    bracket_repair_retcode/10025     1

One finding came out of building this and is recorded in `CLAUDE.md`: all
eight `10016` refusals on an ORDER fall in the single minute 21:00-21:01 UTC,
and every order attempt in that minute was refused -- 8 of 8, against 2 of
1,141 at every other minute. 21:00 UTC is 00:00 at this broker, which is
rollover, where the quote band is already measured as wider than the bracket.
So 10016 here is a venue-state window, not a geometry fault in the request.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Cls(str, Enum):
    """What the caller must DO. A class nobody branches on is decoration."""

    #: The venue did it. Do not resend -- a resend duplicates. Confirm by
    #: re-reading the book, because a retcode is not a confirmation.
    DONE = "DONE"
    #: The venue evaluated the request and objected to it. Nothing exists at
    #: the venue. Stop looping; a corrected request may be sent.
    REFUSED_REQUEST = "REFUSED_REQUEST"
    #: The venue objected to ITSELF, not to the request. Nothing exists at the
    #: venue. Stop looping and report upward with a time: 10018 reopens,
    #: 10017 does not.
    REFUSED_VENUE_STATE = "REFUSED_VENUE_STATE"
    #: The venue changed nothing because the requested state already held.
    #: NOT a failure, and treating it as one is how a correct bracket got a
    #: position closed out from under it.
    MOOT = "MOOT"
    #: No verdict came back. Transmission unknown. On a CLOSE, retry inside
    #: the wall-clock budget. On an OPEN, do not resend -- reconcile first.
    NO_ANSWER = "NO_ANSWER"
    #: Seen, never explained here. Surface the integer and re-read the book.
    #: Never silently retried and never silently treated as terminal.
    UNCLASSIFIED = "UNCLASSIFIED"


class Retry(str, Enum):
    NEVER = "NEVER"
    #: Futile as sent; a corrected request is fine.
    AFTER_CHANGE = "AFTER_CHANGE"
    #: The venue will accept it later, on the calendar's schedule.
    AFTER_WAIT = "AFTER_WAIT"
    NOW = "NOW"
    UNKNOWN = "UNKNOWN"


class Tri(str, Enum):
    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Verdict:
    code: int | None
    name: str | None
    cls: Cls
    retry: Retry
    #: Did the request reach the server?
    transmitted: Tri
    #: Did it create something at the venue? This is the field that decides
    #: whether a resend can open a second position.
    booked: Tri
    #: Where in this repository the row is justified.
    evidence: str

    @property
    def is_terminal(self) -> bool:
        return self.cls in (Cls.DONE, Cls.REFUSED_REQUEST, Cls.REFUSED_VENUE_STATE, Cls.MOOT)

    @property
    def safe_to_resend(self) -> bool:
        """Only when nothing was booked AND we know nothing was booked."""
        return self.booked is Tri.NO


DONE = 10009
INVALID_STOPS = 10016
TRADE_DISABLED = 10017
MARKET_CLOSED = 10018
NO_CHANGES = 10025
UNSUPPORTED_FILLING = 10030
NO_CONNECTION = 10031

TABLE: dict[int, Verdict] = {
    DONE: Verdict(
        DONE, "TRADE_RETCODE_DONE", Cls.DONE, Retry.NEVER,
        Tri.YES, Tri.YES,
        "tools/mt5_paper.py:577; 1964 rows in data/paper_trades.jsonl",
    ),
    INVALID_STOPS: Verdict(
        INVALID_STOPS, "TRADE_RETCODE_INVALID_STOPS", Cls.REFUSED_VENUE_STATE,
        Retry.AFTER_WAIT, Tri.YES, Tri.NO,
        "10 rows; 8 of them inside 21:00-21:01 UTC = 00:00 server, rollover",
    ),
    TRADE_DISABLED: Verdict(
        TRADE_DISABLED, "TRADE_RETCODE_TRADE_DISABLED", Cls.REFUSED_VENUE_STATE,
        Retry.NEVER, Tri.YES, Tri.NO,
        'data/paper_trades.jsonl: DE40 2026-08-31, comment "Trade disabled"',
    ),
    MARKET_CLOSED: Verdict(
        MARKET_CLOSED, "TRADE_RETCODE_MARKET_CLOSED", Cls.REFUSED_VENUE_STATE,
        Retry.AFTER_WAIT, Tri.YES, Tri.NO,
        "82 close rows; logs/overnight-20260911-225029.log:1349-1397",
    ),
    NO_CHANGES: Verdict(
        NO_CHANGES, "TRADE_RETCODE_NO_CHANGES", Cls.MOOT, Retry.NEVER,
        Tri.YES, Tri.NO,
        "data/paper_trades.jsonl: one bracket_repair_retcode, EURUSD 2026-09-07",
    ),
    UNSUPPORTED_FILLING: Verdict(
        UNSUPPORTED_FILLING, "TRADE_RETCODE_INVALID_FILL", Cls.REFUSED_REQUEST,
        Retry.AFTER_CHANGE, Tri.YES, Tri.NO,
        'first line of the ledger, USDCAD, "Unsupported filling mode"; '
        "cause fixed in mt5_paper.filling_for()",
    ),
    NO_CONNECTION: Verdict(
        NO_CONNECTION, "TRADE_RETCODE_NO_CONNECTION", Cls.NO_ANSWER,
        Retry.NOW, Tri.UNKNOWN, Tri.UNKNOWN,
        "85 rows; tools/take_profit.py flush retry loop exists for it",
    ),
}

#: Observed once, with an empty venue comment, and never explained here.
#: Left deliberately unnamed and unclassified rather than guessed at.
UNEXPLAINED: dict[int, str] = {
    10036: "data/paper_trades.jsonl: NZDUSD 2026-09-08, close FAILED, no comment",
}


def classify(res: object, *, send_error: str | None = None, action: str = "open") -> Verdict:
    """Read a send result. Never takes a bare integer, and that is deliberate.

    The absent-retcode case is not one condition but several, and they mean
    opposite things. An exception raised at `order_send` and a clean venue
    refusal both arrive with no usable retcode, and one of them may have
    reached the server while the other provably did not. A function taking
    `int | None` cannot tell them apart, so it would have to guess exactly
    where guessing is most expensive.

    `action` matters for one code. On a CLOSE, `NO_CONNECTION` is retryable
    now -- a failed close leaves the position where it already was. On an
    OPEN it is not, because the order may have landed and a resend would open
    a second position. Same code, different instruction.
    """
    if send_error is not None:
        return Verdict(
            None, None, Cls.NO_ANSWER, Retry.UNKNOWN,
            Tri.UNKNOWN, Tri.UNKNOWN,
            f"no retcode; the send itself failed: {send_error[:120]}",
        )

    code = getattr(res, "retcode", None)
    if code is None:
        return Verdict(
            None, None, Cls.NO_ANSWER, Retry.UNKNOWN,
            Tri.UNKNOWN, Tri.UNKNOWN,
            "no retcode on the result object",
        )

    verdict = TABLE.get(int(code))
    if verdict is None:
        return Verdict(
            int(code), None, Cls.UNCLASSIFIED, Retry.NEVER,
            Tri.YES, Tri.UNKNOWN,
            UNEXPLAINED.get(int(code), "not observed in this repository before"),
        )

    if verdict.cls is Cls.NO_ANSWER and action == "open":
        # A close that did not answer can be retried: the position is still
        # there and closing it twice is not a second position. An OPEN that
        # did not answer cannot, and the difference is a second lot at the
        # venue that nobody intended.
        return Verdict(
            verdict.code, verdict.name, verdict.cls, Retry.UNKNOWN,
            verdict.transmitted, verdict.booked,
            verdict.evidence + "; retry withheld because the action was an open",
        )
    return verdict


def classify_row(row: dict) -> Verdict:
    """Classify a RECORDED result row, not a live send.

    Separate from `classify()` on purpose. That one refuses a bare integer
    because an exception at `order_send` and a venue refusal both arrive
    without a usable retcode and mean opposite things. In a recorded row that
    ambiguity is already resolved and written down: `status` is `UNKNOWN` when
    the send itself failed and `REJECTED`/`FAILED` when the venue answered. So
    the row carries what the bare integer lacked, and reading it back is safe
    where re-deriving it from the code alone would not be.
    """
    if row.get("status") == "UNKNOWN":
        return Verdict(
            row.get("retcode"), None, Cls.NO_ANSWER, Retry.UNKNOWN,
            Tri.UNKNOWN, Tri.UNKNOWN,
            f"recorded UNKNOWN: {str(row.get('send_error') or '')[:120]}",
        )

    class _Recorded:
        retcode = row.get("retcode")

    return classify(_Recorded(), action=str(row.get("action") or "close"))


def describe(verdict: Verdict) -> str:
    """One operator-facing line. The integer is always present in it."""
    name = verdict.name or "unnamed"
    code = verdict.code if verdict.code is not None else "none"
    return (
        f"retcode {code} ({name}): {verdict.cls.value}, "
        f"retry={verdict.retry.value}, transmitted={verdict.transmitted.value}, "
        f"booked={verdict.booked.value}"
    )
