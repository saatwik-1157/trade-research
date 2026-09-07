# LEVEL_64_TEST_REPORT.md

Adaptive policy research and controlled policy evolution. 2026-09-06.

---

# Certification: CONDITIONALLY_CERTIFIED · Autonomy: BOUNDED (3)
# Active policy: the module constants · Challenger policies: none

Unchanged from L63. No candidate has been generated, so there is no challenger
and nothing has been promoted.

---

## 1. The design decision the level rests on

**Research operates on an ALLOW-LIST. Everything else in the safety package
uses blocklists, and the difference is deliberate.**

L62's `ENVELOPE_TARGETS` and L63's `GOVERNANCE_TARGETS` name what an autonomous
*action* may not touch. That polarity is correct there: an action's targets are
known when the action is written.

It is exactly wrong for research. A blocklist means *everything is researchable
unless forbidden* — so a safety parameter added next month would be researchable
by default, by nobody's decision.

> **Anything unrecognised is `HARD_SAFETY`.**

`max_portfolio_risk`, `risk_engine_enabled` and `live_trading` are refused not
because somebody remembered to forbid them, but because nobody authorised them.
`test_anything_unrecognised_is_hard_safety` proves it with two parameter names
invented for the test.

**Capability-checked:** flipping the default from `HARD_SAFETY` to `GOVERNANCE`
fails twelve tests.

## 2. Section 41 — the critical risk test

> No optimizer, research engine, AI model, policy engine, or portfolio
> controller may convert $10,000 approved risk into $15,000 approved risk.

**Refused at static validation, before any simulation runs.** A capital ceiling
is Category A, and Category A has no approval path — not "rejected after
review", but *not reviewable*.

## 3. Section 40 — the fifteen mandatory tests

| | Test | Result |
|---|---|---|
| A | Candidate raises maximum risk | REJECTED_HARD_SAFETY |
| B | Candidate raises leverage | REJECTED_HARD_SAFETY |
| C | Candidate disables the RiskEngine | REJECTED_HARD_SAFETY |
| D | Candidate bypasses certification | REJECTED_HARD_SAFETY |
| E | AI generates an unsafe policy | refused as data, never run |
| F | Strong in-sample, weak out-of-sample | loses; delegated to the L58/59 gate |
| G | Identical to a rejected policy | REJECTED_DUPLICATE |
| H | Causes control-loop instability | REJECTED_OUT_OF_BOUNDS |
| I | Causes excessive action frequency | REJECTED_OUT_OF_BOUNDS / HARD_SAFETY |
| J | Passes research, fails shadow safety | cannot reach CERTIFIED |
| K | Approved policy fails during canary | CANARY → ROLLED_BACK, terminal |
| L | Rollback target unavailable | every state can reach RETIRED |
| M | Research loses database access | same — no state only promotes forward |
| N | AI unavailable | nothing imports the module; certified policy stands |
| O | Candidate attempts direct MT5 access | cannot express it; it is not code |

All fifteen pass.

**TEST E and TEST O share an answer worth stating plainly.** Section 9 asks for
a sandbox preventing broker access, filesystem abuse, secret access and
arbitrary network access. The stronger answer is not a better sandbox: a
`Candidate` is `dict[str, Decimal]`, there is no interpreter for it, and every
item on that list is *unreachable from a dictionary of decimals* rather than
blocked. A sandbox would be a guard at a door that does not exist, and its
presence would imply the door does.

## 4. Test results

| Suite | Tests | Result |
|---|---|---|
| `tests/test_policy_research.py` (new) | 33 | pass |
| `tests/test_autonomy_governance.py` | 48 | pass |
| `tests/test_safety_invariants.py` | 66 | pass |
| `tests/test_policy_verification.py` | 37 | pass |
| **Four safety suites** | **184** | **pass** |
| **Whole backend, 12 batches** | **2,767** | **0 failed, 6 skipped** |

`ruff check` clean · `ruff format` clean · `mypy` clean over 315 source files.

Invariants: **28 — 26 ENFORCED, 2 NOT_APPLICABLE, 0 UNENFORCED.** INV-28 added
(hard safety is not researchable), under GATE-03.

## 5. Architecture inspection (section 47)

- `test_only_the_oms_reaches_a_venue_to_write` — **pass.** Still exactly two
  modules call a venue write: `app/oms/service.py` and one `self.modify_order`
  in `app/brokers/mt5.py`.
- No policy → MT5 path. No AI → policy deployment path: `policy_version` is a
  governance target (L63), so an action aimed at it is FORBIDDEN with no
  approval path.
- No risk expansion possible: verified at both the action layer (L62 envelope)
  and the research layer (Category A).
- `trading_mode=paper`, `live_trading=false`, `live_execution_allowed=false` on
  the running deployment.

## 6. What existed / reused / modified / added / rejected

**Reused unchanged, and the important one first:** `app/research/selection.py`
— the L58/L59 multiple-testing gate — covers sections 25 and 26. **It was
validated against this repository's own recorded searches** (`CLAUDE.md`
reports 41 candidates clearing ~1 by chance, 128 clearing 3.2, 200 clearing
4.9; the gate reproduces all three). Rebuilding it would have been a second
implementation of the one piece of research machinery this project has proven
against real data.

Also reused: `app/brokers/shadow.py`, `app/portfolio/control.py`,
`app/portfolio/decision.py`, the whole L62/L63 safety package,
`app/datasets/leakage.py`.

**Modified:** `app/safety/invariants.py` (INV-28),
`app/safety/certification.py` (GATE-03).

**Added:** `app/safety/policy_research.py`, `tests/test_policy_research.py`,
fifteen documents.

**Rejected:** candidate generation, historical policy replay, the
counterfactual harness, stress testing, walk-forward policy evaluation, shadow
policy runs, policy effectiveness measurement, the challenger registry,
persistent research memory, compute limits, database tables, APIs, and the
frontend research dashboard.

## 7. Research results

**None, and that is the honest entry.** Zero candidates generated, zero
hypotheses evaluated, zero challengers. The machinery that decides *what may be
researched* is built; the machinery that *does the researching* is not.

The reason is the same throughout: every declined item needs a governance
policy that has actually run to produce the data it would evaluate. There is no
policy deployment history, no autonomous action history and no effectiveness
record, because no autonomous action has ever been applied on this platform.

Generating candidates and scoring them against nothing would produce a ranked
list with confidence intervals over an empty sample — which is precisely the
failure `CLAUDE.md` documents at length across five universes and four search
families, where the winner never survived out-of-sample. Running that machinery
over zero observations would not be a smaller version of that mistake; it would
be a purer one.

## 8. Remaining risks

1. **The allow-list is hand-maintained.** Eight entries, capped at twelve by a
   test. Its *failure mode is safe* — forgetting to classify something makes it
   immutable, not researchable — but a legitimate research target stays
   unavailable until somebody adds it.
2. **Bounds are configuration decisions, not measurements.** No governance
   policy has ever run, so there is no observed distribution to fit. They
   require approval.
3. **Research memory is process-local.** `seen` is a caller-supplied dict, so a
   restart forgets what was already assessed. Bounded today: zero candidates
   exist. When they do, this needs a table.
4. **The candidate type is the safety property.** `dict[str, Decimal]` is what
   makes the sandbox question dissolve. If a future level needs expressions
   rather than numbers, the entire sandbox requirement returns and
   `POLICY_SANDBOX_SECURITY.md` stops applying.
5. **The architecture scan is syntactic** — unchanged from L62.
6. **Unchanged and still the top of the list:** the MT5 adapter has never
   connected and the backup has never been restored. Four levels have now
   certified, verified, governed and researched the policy around a component
   that has never run.

## 9. Unresolved

Nothing found during L64 was left unfixed. One imprecision was found and
corrected during the level: the import guard for TEST N originally grepped
text, and fired the moment the invariant registry *named* the research module
in a string — a reference, not a dependency. It now walks the syntax tree.
**Match the structure, never the characters** — the same lesson as C-4 and the
L57 lifecycle guard.
