"""The model lifecycle: which states exist, and which moves between them are legal.

**This is not a second registry.** `app/ai/registry.py` holds *loaded model
objects* in process and answers `get(key, version)`; `model_versions` has been
the one table since L05. This module owns the `status` column on that table and
nothing else — the state machine, its transitions, and the audit trail they
leave. Section 47 asks that no second registry, loader or version table appear,
and none does.

**Eight states, and every one is reachable.** The brief lists ten; four of them
are folded or declined, each for a stated reason (see `DECLINED` below). The
rule this project has applied since L22 holds: a state nothing can enter makes
the vocabulary a wish list, and a reader cannot tell an unused state from an
unreachable one.

**Transitions are a table, not a series of `if`s.** `TRANSITIONS` is the whole
truth about what may follow what, so a new state cannot acquire an accidental
edge and a test can enumerate every path.

**Nothing here promotes to live.** Section 12: there is no transition that
enables live trading, no code path from this module to a broker, and `promoted`
means "this is the version the scope resolves to" — under `TRADING_MODE=paper`
and `LIVE_TRADING=false`, which L28 does not touch. A test asserts the module
imports nothing that could execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class LifecycleError(Exception):
    """A transition that is not legal. Refused, never coerced into one that is."""


class ModelStatus(StrEnum):
    """Where a model version is in its life.

    The four existing values keep their spellings — `draft`, `validated`,
    `promoted`, `retired` have been in the CHECK constraint since L05 and are
    written by L24 and L25. Section 10 asks to use existing naming where it
    exists, and renaming `promoted` to `active` would rewrite history for a
    synonym.
    """

    # A candidate. Written by `ai_service.register_version` when training ends;
    # this is the brief's CANDIDATE under its existing name.
    draft = "draft"
    # L26's validation returned PASS or CONDITIONAL. A statement about the
    # EVIDENCE, and nothing more.
    validated = "validated"
    # The registry accepted it: the artifact loads and its digest matches, the
    # lineage is complete, the feature contract is compatible. A statement about
    # the ARTEFACT, which is a different check and can fail independently.
    registered = "registered"
    # Deployed to paper trading for a scope.
    paper = "paper"
    # The version a scope resolves to. The brief's ACTIVE, under the spelling
    # this table has used since L05.
    promoted = "promoted"
    # Refused: validation failed or was blocked, or the registry's own checks
    # did not pass. Terminal, and it is not a deletion -- §22.
    rejected = "rejected"
    # Withdrawn after having been deployed, because something was wrong with it.
    # Distinct from `retired`: one says "we stopped using this deliberately",
    # the other says "we stopped using this in a hurry", and an operator reading
    # the history a month later needs both.
    rolled_back = "rolled_back"
    # Deliberately withdrawn. Terminal, and never a deletion.
    retired = "retired"


#: States from which inference may be served. Section 29.
#:
#: `validated` is deliberately ABSENT. Validation says the evidence supports the
#: candidate; it says nothing about whether the artifact loads or whether its
#: feature contract still matches. Both are checked at registration, and serving
#: inference from a version that has passed only the first is the gap §16 exists
#: to close.
SERVING_STATUSES: frozenset[ModelStatus] = frozenset(
    {ModelStatus.registered, ModelStatus.paper, ModelStatus.promoted}
)

#: States a version can never leave.
TERMINAL: frozenset[ModelStatus] = frozenset({ModelStatus.rejected, ModelStatus.retired})

#: The whole truth about what may follow what.
#:
#: Read the shape rather than the entries: everything flows toward `retired`,
#: nothing flows back out of it, and there is exactly one edge into `promoted`
#: — from `paper`. That single edge is §12: a newly trained model cannot reach
#: the active state without having been deployed to paper first, because there
#: is no arc that would let it.
TRANSITIONS: dict[ModelStatus, frozenset[ModelStatus]] = {
    ModelStatus.draft: frozenset({ModelStatus.validated, ModelStatus.rejected}),
    ModelStatus.validated: frozenset({ModelStatus.registered, ModelStatus.rejected}),
    ModelStatus.registered: frozenset(
        {ModelStatus.paper, ModelStatus.retired, ModelStatus.rejected}
    ),
    # `registered` is reachable from both deployed states, and it is what a
    # deployment ENDING means: superseded by a newer version, or stopped by an
    # operator. The version is unchanged and still eligible -- retiring it
    # instead would make every rollback a resurrection, which §22 says should be
    # a deliberate separate act.
    ModelStatus.paper: frozenset(
        {ModelStatus.promoted, ModelStatus.registered, ModelStatus.rolled_back, ModelStatus.retired}
    ),
    ModelStatus.promoted: frozenset(
        {ModelStatus.registered, ModelStatus.rolled_back, ModelStatus.retired}
    ),
    # A rolled-back version is not condemned. It goes back to the eligible pool
    # so it can be re-deployed once whatever went wrong is understood -- or it
    # is retired deliberately. What it cannot do is jump straight back to
    # `promoted`, which would be the blind re-activation §20 forbids.
    ModelStatus.rolled_back: frozenset({ModelStatus.registered, ModelStatus.retired}),
    ModelStatus.rejected: frozenset(),
    ModelStatus.retired: frozenset(),
}

#: The brief's states that were NOT added, each with the reason.
#:
#: Recorded here rather than omitted silently, because "we thought about it and
#: decided against" and "we forgot" look identical in a schema.
DECLINED: dict[str, str] = {
    "CANDIDATE": (
        "already exists as `draft`, written by `register_version` since L24. §10 asks "
        "to use the existing naming, and a second word for one state is how a reader "
        "comes to believe they are two."
    ),
    "VALIDATING": (
        "nothing can set it. L26 deliberately writes no model status -- a PASS makes a "
        "candidate eligible for CONSIDERATION and nothing more -- and `validation_runs` "
        "already records that a run is `running`. Adding it here would put the same "
        "fact in two places, and the copy nobody updates is the one somebody reads."
    ),
    "FAILED": (
        "a validation that failed is recorded on `validation_runs.status`; the VERSION "
        "it judged becomes `rejected`. Two terminal failure states on the version would "
        "differ only in which system said no."
    ),
    "ACTIVE": (
        "already exists as `promoted`, in the CHECK constraint since L05. Renaming it "
        "would rewrite the history of every row that ever held it."
    ),
}


def can_transition(current: ModelStatus, target: ModelStatus) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def check_transition(current: ModelStatus, target: ModelStatus) -> None:
    """Refuse an illegal move, and say what would have been legal.

    Raises rather than returning a bool at the call sites that matter, so a
    caller cannot forget to look at the answer.
    """
    if current is target:
        raise LifecycleError(
            f"the version is already {current}. A no-op transition is refused rather "
            "than recorded, because a history full of them hides the real ones."
        )
    if current in TERMINAL:
        raise LifecycleError(
            f"{current} is terminal. A {current} version is never deleted (§22) and is "
            "never resurrected either: register the retrained model as a new version, "
            "which is what §6's immutability rule means in practice."
        )
    if not can_transition(current, target):
        allowed = ", ".join(sorted(str(s) for s in TRANSITIONS.get(current, frozenset())))
        raise LifecycleError(
            f"{current} -> {target} is not a legal transition. From {current} a version "
            f"may go to: {allowed or '(nothing; terminal)'}."
        )


def serves_inference(status: str) -> bool:
    """Whether a version in this status may answer a prediction. Section 29."""
    try:
        return ModelStatus(status) in SERVING_STATUSES
    except ValueError:
        # An unrecognised status is not a serving one. A status this deployment
        # does not know is a row written by code this deployment does not have,
        # and guessing what it meant is how an unapproved model gets consulted.
        return False


@dataclass(frozen=True)
class Transition:
    """One legal move, with the reason it was made. Section 19."""

    version_id: str
    model_key: str
    from_status: ModelStatus
    to_status: ModelStatus
    reason: str
    actor_user_id: str | None = None
    environment: str = "paper"
    validation_run_id: str | None = None
    deployment_id: str | None = None
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "model_key": self.model_key,
            "from": str(self.from_status),
            "to": str(self.to_status),
            "reason": self.reason,
            "actor_user_id": self.actor_user_id,
            "environment": self.environment,
            "validation_run_id": self.validation_run_id,
            "deployment_id": self.deployment_id,
            "details": self.details or {},
        }


def describe() -> dict[str, Any]:
    """The lifecycle as data, for the API and the docs to render.

    Served rather than restated in either, so a page and a document cannot drift
    from the transition table the code enforces.
    """
    return {
        "statuses": {
            str(ModelStatus.draft): "a trained candidate. Written when training ends.",
            str(ModelStatus.validated): (
                "L26's validation returned PASS or CONDITIONAL. A statement about the "
                "evidence, and nothing more."
            ),
            str(ModelStatus.registered): (
                "the registry accepted it: the artifact loads, its digest matches, the "
                "lineage is complete and the feature contract is compatible. A "
                "different check from validation, which can fail independently."
            ),
            str(ModelStatus.paper): "deployed to paper trading for a scope.",
            str(ModelStatus.promoted): "the version a scope resolves to.",
            str(ModelStatus.rejected): "refused. Terminal, and never a deletion.",
            str(ModelStatus.rolled_back): (
                "withdrawn because something was wrong. Distinct from retired: one is "
                "deliberate, the other is in a hurry, and the history needs both."
            ),
            str(ModelStatus.retired): "deliberately withdrawn. Terminal, never a deletion.",
        },
        "transitions": {
            str(current): sorted(str(s) for s in allowed)
            for current, allowed in TRANSITIONS.items()
        },
        "serving": sorted(str(s) for s in SERVING_STATUSES),
        "terminal": sorted(str(s) for s in TERMINAL),
        "declined": DECLINED,
        "guarantees": [
            "there is exactly ONE edge into `promoted`, and it comes from `paper`. A "
            "newly trained model cannot reach the active state without having been "
            "deployed to paper first, because no arc would let it.",
            "`validated` does not serve inference. Validation says the evidence supports "
            "the candidate; registration says the artifact loads and still matches its "
            "feature contract, and both must hold.",
            "a rolled-back version returns to `registered`, never straight to "
            "`promoted`. Re-activating it is a second deliberate act.",
            "nothing is ever deleted. `rejected` and `retired` are terminal states, not "
            "removals, because a historical version is needed for audit, backtesting, "
            "trade review and reproducibility.",
        ],
        "does_not": [
            "enable live trading, or change TRADING_MODE or LIVE_TRADING",
            "promote a newly trained model to active without a paper deployment",
            "bypass L26's validation gate",
            "reach the risk engine, position sizing, the OMS or a broker adapter",
            "delete a model version, its artifact or its history",
        ],
    }
