"""Which strategy-version status may follow which. **L57.**

`strategy_versions.status` is a lifecycle, and it was **the one lifecycle in
this platform with no transition guard.** `app/api/v1/strategy_builder.py`
checked the DESTINATION -- is it reachable, is it blocked until a later level,
does the definition still validate -- and never the (from, to) pair:

    version.status = wanted

So `retired -> validated` was permitted. A retired strategy could be brought
back with one call, and L57 step 21 says exactly why that is wrong:
reactivation must require current validation, current dependencies, current
model status, current market-data compatibility and current approval. **"Do
not blindly reactivate an old strategy."**

The platform already knows how to do this. `app/oms/state.py`,
`app/bots/state.py`, `app/risk/state.py` and `app/portfolio/state.py` each hold
a `TRANSITIONS` table and a `check_transition` that raises. This module is the
same pattern for the one place that lacked it -- **not a new mechanism, the
existing one applied where it was missing.**

**`retired` is terminal, and that is the decision worth stating.** Reactivating
means creating a NEW version, which is the only path that forces the evidence
step 21 lists: a new version is drafted, re-validated against the current
platform, and approved on its own merits. Allowing `retired -> validated` would
let a strategy skip all of it by editing one field.

That also keeps step 20's rule intact -- retired means NO NEW DEPLOYMENT, never
delete history. The row, its config, its trades and its audit trail all remain;
what it cannot do is quietly become current again.
"""

from __future__ import annotations

from enum import StrEnum


class VersionStatus(StrEnum):
    """The statuses `strategy_versions.status` may hold.

    Exactly the three the CHECK constraint permits. `backtested`, `paper`,
    `active` and `paused` are deliberately absent: the route already refuses
    them with "the machinery that would enforce it is built at level NN", and
    a status nothing checks is a strategy claiming a state it does not have.
    """

    draft = "draft"
    validated = "validated"
    retired = "retired"


#: What may follow what.
#:
#: `draft -> validated`   the definition passed re-validation on the way in.
#: `draft -> retired`     a draft nobody finished.
#: `validated -> draft`   withdrawing validation. An operator who decides a
#:                        version is not ready after all should not have to
#:                        retire it to say so.
#: `validated -> retired` the ordinary end of a version's life.
#: `retired -> nothing`   TERMINAL. Reactivation is a new version, which is the
#:                        only path that forces re-validation and approval.
TRANSITIONS: dict[VersionStatus, frozenset[VersionStatus]] = {
    VersionStatus.draft: frozenset({VersionStatus.validated, VersionStatus.retired}),
    VersionStatus.validated: frozenset({VersionStatus.draft, VersionStatus.retired}),
    VersionStatus.retired: frozenset(),
}


class IllegalVersionTransition(Exception):
    """A strategy-version status change the lifecycle does not allow."""


def check_transition(current: VersionStatus | str, wanted: VersionStatus | str) -> None:
    """Raise unless `current -> wanted` is a legal move.

    Same shape and same contract as `app/oms/state.py::check_transition`, so a
    reader who knows one knows this. A same-state move is refused rather than
    silently accepted: "set it to what it already is" is a no-op the CALLER
    should recognise, and treating it as a transition would put a meaningless
    row in the audit trail.
    """
    try:
        now = VersionStatus(str(current))
        target = VersionStatus(str(wanted))
    except ValueError as exc:
        raise IllegalVersionTransition(
            f"{current!r} -> {wanted!r} is not a strategy-version transition; the "
            f"statuses are {', '.join(s.value for s in VersionStatus)}"
        ) from exc

    if now == target:
        raise IllegalVersionTransition(
            f"the version is already {target}; setting a status to itself is not a "
            "transition and would leave a record of a change that did not happen"
        )

    allowed = TRANSITIONS[now]
    if target not in allowed:
        options = ", ".join(sorted(s.value for s in allowed)) or "nothing"
        detail = (
            " A retired version is final: bring the strategy back by creating a NEW "
            "version, which is re-validated and approved on its own evidence rather "
            "than inheriting an old decision."
            if now is VersionStatus.retired
            else ""
        )
        raise IllegalVersionTransition(
            f"a {now} version cannot become {target}; it may become {options}.{detail}"
        )


__all__ = [
    "TRANSITIONS",
    "IllegalVersionTransition",
    "VersionStatus",
    "check_transition",
]
