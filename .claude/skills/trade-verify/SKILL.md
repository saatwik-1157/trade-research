---
name: trade-verify
description: Check that every number in a written research note traces back to computed data. Use after writing any report, or to audit a note whose figures are in doubt.
---

# Verify the numbers in a report

```bash
python tools/verify.py reports/TICKER.report.md reports/TICKER.snapshot.json --show-verified
```

Each numeric claim in the prose is matched against the snapshot and attributed
to the field it came from. Anything unmatched is listed.

An unverified number is not automatically wrong - it may be sourced inline from
a filing or an article. It is a number that needs a citation or removal.

Two limits worth stating when you report results:

- Matching is on value alone, so a figure can occasionally be attributed to an
  unrelated field holding the same number. It establishes that a figure exists
  in the data, not that it means what the sentence claims.
- It checks numbers, not reasoning. A note can pass with every figure correct
  and still draw a conclusion the figures do not support.

Add `--strict` to make it exit non-zero when anything is unverified, which is
what the `trade-analyze` workflow uses as a gate.
