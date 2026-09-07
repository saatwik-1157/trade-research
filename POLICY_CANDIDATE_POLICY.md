# POLICY_CANDIDATE_POLICY.md

L64 sections 8 and 11. What a candidate is, and what makes one valid.

---

## A candidate is data

`Candidate.changes` is `dict[str, Decimal]`. Nothing else. No expression, no
callable, no source string.

That is the whole of section 8's "never execute generated policy code" and
section 9's sandbox requirement: **there is nothing executable to run**, so
broker access, filesystem abuse, secret access and arbitrary network access are
not prevented, they are unreachable.

AI-generated candidates are treated as untrusted input in the only way that
matters — they are parsed as numbers and validated against bounds, exactly like
any other candidate. There is no privileged path.

## Static validation runs every check

`validate()` collects **every** reason a candidate is refusable, not the first.
An author reading a rejection wants the whole list; a validator that stops at
the first failure turns one fix into five round trips.

Verdicts: `ACCEPTED` · `REJECTED_HARD_SAFETY` · `REJECTED_OUT_OF_BOUNDS` ·
`REJECTED_MALFORMED` · `REJECTED_DUPLICATE`.

## Bounds

Every researchable parameter has an inclusive range, and the ranges are
**themselves Category A**. A candidate proposing to widen a bound is proposing
to change the rules it is judged by, which is the same move L62 blocked for the
safety envelope and L63 for the certification machinery.

The values are configuration decisions, not measurements. No governance policy
has ever run here, so there is no observed distribution to fit. **They require
approval.**

## Duplicate detection

The fingerprint covers the parent policy version and the changed
parameter/value pairs. It **excludes** the candidate id and the rationale: two
candidates differing only in how they were described are not two policies, and
including either would make deduplication useless.

A different parent version *is* a different candidate — the same numbers under
different rules have not actually been assessed.

Re-testing an already-assessed candidate is refused as *"another draw from the
same urn"*, which is section 25's multiple-testing concern stated at the point
where it bites.
