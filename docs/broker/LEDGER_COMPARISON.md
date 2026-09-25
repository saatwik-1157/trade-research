# Ledger comparison: does the platform's record agree with the venue?

2026-09-07. The measurement this whole exercise was for.

Every other check in this platform compares the platform against itself, and the
whole of today has shown what that misses: `realized_pnl` said `0.0000`, every
reader of the row agreed, and the account had received `-0.04`. This asks the
one question that cannot be answered from the inside — for each position the
platform believes it opened, does **MetaTrader's own deal history** say the same
thing about the entry, the size, the exit and the money?

## The command

```bash
cd backend
python -m app.brokers.venue_audit --mode demo
python -m app.brokers.venue_audit --mode demo --json
python -m app.brokers.venue_audit --mode demo --strict   # exit 1 on any disagreement
```

Read-only on both sides, and a test parses the module to prove it: no `add`, no
`commit`, no `order_send`. **A disagreement is a finding for a person.** The
instinct on discovering one is to correct a side, and both directions of that
instinct are wrong — the venue is authoritative about what it did, and only our
record knows what we intended.

Windows only, like everything that reads a terminal.

## Why it is not reconciliation

`app/brokers/reconcile.py` asks *"does the venue still hold what we think it
holds"* — a question about **open** positions, answered against the venue's
current book.

This asks *"was what we recorded true"* — a question about **closed** ones,
answered against the deal history. A position can reconcile perfectly all the
way to its close and still have been booked at the wrong price. That is exactly
what happened to four of the nine below.

## The result

```
  9 position(s) checked, 5 agree with the venue
```

| position | opened | source | disagreement |
|---|---|---|---|
| 58326177606 | 06:04 | manual | `status` open vs closed; `realized_pnl` — vs **0.54** |
| 58328592839 | 08:18 | manual | `realized_pnl` — vs **0.33** |
| 58328777371 | 08:26 | manual | `realized_pnl` — vs **−0.03** |
| 58328827918 | 08:29 | manual | `realized_pnl` **0.0000** vs **−0.04** |
| 58329828067 | 09:28 | manual | **agrees** |
| 58329837257 | 09:28 | manual | **agrees** |
| 58330565300 | 10:16 | manual | **agrees** |
| 58331472838 | 11:02 | manual | **agrees** |
| 58332563074 | 12:00 | pipeline | **agrees** |

**The boundary is chronological and exact.** Every disagreement predates the fix
for it; every trade made after the fixes agrees with the venue, including the one
the signal path produced.

Each of the four is a defect this session found and fixed, still visible in the
record it left behind:

* **58326177606** — the harness harvested a platform position out of band
  (shared `MAGIC 770315`), and the row stayed `open`. It is also the orphan with
  a NULL `broker_account_id`, which the account-scoped sweep could not reach:
  `_local` scopes by account, so a row matching no account never appeared at
  all. Since L70m the sweep **reports** it under `unattributed` and is not
  `clean` while it stands. It is still open in the platform and closed at the
  venue — being seen is what was fixed, not being resolved.
* **58328592839** — the close was never confirmable: `close_own` recorded a
  retcode and not the fill, so the OMS parked it `unknown` and the money was
  never booked.
* **58328777371** — the close executed and the row write rolled back, on the
  aware-datetime defect.
* **58328827918** — the money defect itself: a price difference where the
  account currency belonged.

Nothing was written to correct any of them. They are the record of what
happened.

## What this does not compare

**The toolkit's own ledger.** `data/track_record.jsonl` holds 252 positions and
**none** of these nine: its last merge predates today. See
"What the ledger turned out to be" below — the two are the same question asked
of the other system.

**Slippage, latency and partial fills.** One order, one fill, every time so far.
Nine positions is not a sample of anything.

## What the ledger turned out to be

The audit above asks whether the platform's record is true. Asking the same
question of the *research* ledger found something worse, and it is not about the
platform at all.

`mt5_account.closed_trades` pairs every deal on the **account**. The toolkit's
ledger has always been described as this tool's trading record, but what it
merges is *everything that closed*, so any trade opened by hand in the terminal,
by the platform's adapter, or by any other EA arrives in it indistinguishable
from one the harness made. Until this evening nothing on the row could tell them
apart, because the entry deal's magic was never recorded.

That is not hypothetical. Over the last 30 days on this account:

```
closed trades: 258
by magic:      {0: 1, 770315: 256, 770316: 1}
```

The `0` is **58323811275**, AUDCAD long, opened 11:05 and closed 11:23 today for
**−0.17** — a trade opened by hand, tagged by nobody, and larger than any single
trade the platform made. It is not in the ledger only because the last merge
predates it. The next merge would have added it to a 252-trade research sample
as one of this tool's own, and every statistic in
`reports/track_record.json` would have quietly described it.

**Fixed.** `closed_trades` now records the **entry** deal's magic — the entry,
because a close can legitimately carry a different tag when the server stops a
position out. `merge` takes a `magic` argument defaulting to the harness's own
`770315`, `--all-magics` restores the old behaviour explicitly, and every run
prints the account census beside what it took:

```bash
python tools/track_record.py --merge                    # this tool's trades
python tools/track_record.py --merge --all-magics       # every trade on the account
python tools/track_record.py --merge --exclude 58326177606 58328592839
```

### The tag split is not retroactive

The default excludes **2 of the 258**. It cannot do better, and the reason is
the honest limit of this fix: the adapter shared `770315` until the split, so
**nine of the ten** trades the platform made at this venue today carry the
harness's tag. Only `58334342528` (21:59) is separable by magic.

| platform position | time | source | magic | separable by tag |
|---|---|---|---|---|
| 58326177606 | 06:04 | manual | 770315 | no |
| 58328592839 | 08:18 | manual | 770315 | no |
| 58328777371 | 08:26 | manual | 770315 | no |
| 58328827918 | 08:29 | manual | 770315 | no |
| 58329828067 | 09:28 | manual | 770315 | no |
| 58329837257 | 09:28 | manual | 770315 | no |
| 58330565300 | 10:16 | manual | 770315 | no |
| 58331472838 | 11:02 | manual | 770315 | no |
| 58332563074 | 12:00 | pipeline | 770315 | no |
| 58334342528 | 21:59 | manual | 770316 | **yes** |

They are separable by **identity** instead, which is why `--exclude` takes
position ids: the platform's own `positions` table lists exactly which tickets
were its, so the list above is a query result rather than a guess at a time
window.

**The merge was run**, after the decision was taken explicitly: the harness's own
tag, plus `--exclude` for the nine platform trades that carry it. 252 trades
added, 2 excluded by tag, 9 by id; the ledger stands at 504.

### And it exposed a second defect, older than any of the above

The 504 are **two different demo accounts.**

```
tickets 10167310954..10312501236   Aug 24 - Sep 02   252 trades   net -22.37
tickets 58307384869..58336547279   Sep 04 - Sep 07   252 trades   net +31.87
ids in common: 0
```

The report had printed the sum, **+9.50**, as one number. A position id is
unique per account rather than globally, so a second account's trades merge in
cleanly with nothing colliding and nothing to say they are not one record.
Structurally the metals-points error again, wearing an account number.

`merge()` now records the account on every row and the report raises a data gap
and breaks the ledger down by account whenever it spans more than one. The
existing rows were attributed by asking the venue which of them appear in **its
own deal history** — a verification rather than an inference from the ticket
range. The older 252 remain `unrecorded`: that login is not knowable from this
terminal, and a plausible guess is the thing this repository exists to refuse.

## Status

    For every trade the platform has made since its defects were fixed, its
    record agrees with MetaTrader's own history on entry, size, status and
    money.

    Four earlier trades disagree, each for a reason that is now fixed and
    documented. Nothing was rewritten to make the report look clean.
