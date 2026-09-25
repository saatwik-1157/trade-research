# POLICY_RESEARCH_MEMORY.md

L64 section 24. Learning from research history.

---

## What is built

Deduplication. `Candidate.fingerprint()` covers the parent policy version and
the changed parameter/value pairs, and `validate(candidate, seen=...)` refuses
one already assessed, naming the earlier candidate.

The fingerprint deliberately **excludes** the candidate id and the rationale:
two candidates differing only in wording are not two policies. It **includes**
the parent version, because the same numbers under different rules have not
actually been assessed.

## Why deduplication is the memory that matters most

Section 24 asks the system to persist successful hypotheses, failed
hypotheses, rejected policies, failure reasons, evidence, regime-specific
behaviour and known unstable configurations.

Of those, the one that changes an outcome rather than informing a reader is
**not testing the same thing twice**. `CLAUDE.md` documents what repeated draws
from the same urn produce: a 36-cell sweep in which the random rule scored
higher in-sample than any real candidate, and a 200-combination grid where five
cleared against 4.9 expected by chance.

A rejection reason nobody reads costs nothing. A candidate re-tested until it
passes costs the whole result.

## Not built

Persistent storage of research history. `seen` is a caller-supplied dict, so a
process restart forgets. That is a real limitation and it is bounded: the
platform has generated zero candidates, so there is no history to lose yet.

When candidates exist, this needs a table. It does not need one now - the
platform's discipline is that a table arrives with its writer.

## The system must not silently change production policy

Section 24's last line, and it holds structurally: nothing under `app/` imports
the research module, and `policy_version` is a governance target so no
autonomous action can aim at it.
