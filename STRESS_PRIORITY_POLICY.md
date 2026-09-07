# STRESS_PRIORITY_POLICY.md

L67 sections 5 and 6. What to run next.

---

## Never likelihood alone

Section 5's explicit rule, and L66's enumerated risk matrix is reused rather
than recomputed -- so a change to the matrix moves scheduling too, and the two
cannot drift.

An UNLIKELY x EXTREME scenario outranks a LIKELY x EXPECTED one. Multiplying
likelihood by severity is exactly the arithmetic that buries the rare
catastrophe, which is why the matrix is enumerated.

## Being uncovered is worth more than being severe

`priority_of` adds 25 for uncovered and a further 10 for `UNKNOWN` coverage,
against 10 per risk class.

**A severe scenario that has already been run tells you less than a mild one
that never has**, because the first has an answer and the second has none.
`UNKNOWN` scores above `UNCOVERED` because not knowing whether something ran is
worse than knowing it did not.

## Adaptive scheduling changes frequency and nothing else

Section 6. The orchestrator may run correlation stress more often when
correlation risk rises.

It may **not** modify risk limits, leverage, safety policies, certification or
execution permissions -- and it cannot: none of those is reachable from this
module, which imports only L66's scenario vocabulary.
