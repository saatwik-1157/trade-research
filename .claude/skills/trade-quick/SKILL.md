---
name: trade-quick
description: Fast numeric snapshot of a ticker - price, computed indicators, valuation multiples and analyst consensus, with no SEC filings pull and no written narrative. Use for a quick read on a stock.
---

# Quick snapshot

```bash
python tools/snapshot.py TICKER --quick
```

Skips the SEC EDGAR pull and benchmark beta, so it returns in a few seconds
instead of a minute. Report the numbers as they come back.

State plainly that this is the fast path: filed financials were **not**
consulted, so anything about margins, growth or the balance sheet in the output
comes from a vendor summary rather than a filing. For anything that matters,
run `trade-analyze` instead.

Do not write a thesis, a recommendation, or entry and exit levels off a quick
snapshot. Report what was measured.
