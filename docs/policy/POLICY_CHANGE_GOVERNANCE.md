# POLICY_CHANGE_GOVERNANCE.md

How a policy changes. L63 §22, §23 and §24, 2026-09-06.

---

## The rule L63 found broken

**§23: the autonomous system must not modify its own hard safety invariants.**

Before L63 it could get close. An action targeting `autonomy_level`,
`certification_state`, `certification_expiry`, `audit_logging` or `monitoring`
classified as **`REQUIRES_APPROVAL`** — not `FORBIDDEN`. §15 requires a wall
and `REQUIRES_APPROVAL` is a door.

Fixed exactly as L62 fixed the same shape for the safety envelope: by putting
the governance machinery **inside the set L61 already refuses outright**,
rather than by adding a new rule that could be forgotten. `GOVERNANCE_TARGETS`
in `app/safety/autonomy.py`.

The system may now **propose** a policy change. It cannot apply one, approve
one, extend its own certification, raise its own autonomy, or switch off its
own audit log. A system that can disable its own audit log has no audit log.

## The workflow

    POLICY PROPOSAL
    → IMPACT ANALYSIS          (`impact.analyse`, real and running)
    → STATIC VALIDATION        (ruff, mypy)
    → REGRESSION TEST          (`test_safety_invariants`, `test_policy_verification`)
    → REPLAY TEST              (`app.datasets.leakage`, pre-existing)
    → COUNTERFACTUAL TEST      (parameterised conditions)
    → SHADOW EVALUATION        **not built — nothing to shadow**
    → SAFETY REVIEW            human
    → APPROVAL                 human
    → CERTIFICATION            `certify()`
    → CONTROLLED ACTIVATION    staged, see CONTROLLED_RESTORATION_POLICY.md

**There is no `AI → POLICY DEPLOYMENT` edge**, and it is not prevented by a
check — it is prevented by `policy_version` being a governance target, so any
action aimed at it is FORBIDDEN with no approval path.

## What always needs a person

Increasing a risk limit · enabling live trading · changing a hard safety
constraint · approving uncertified autonomy · deploying an unvalidated policy ·
bypassing certification · disabling monitoring or audit logging.

None of these is reachable from an autonomous action at any autonomy level,
including CERTIFIED. They are not gated on autonomy; they are forbidden.

## No interface makes a prohibited action look executable

§22's last line. Nothing in `app/safety/` renders a control, and no route
exposes one — verified by the existing route guards (`SECURITY.md`) rather than
by a second weaker copy of them here.
