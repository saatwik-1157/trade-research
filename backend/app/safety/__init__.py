"""Safety verification and certification. **L62.**

This package VERIFIES. It decides nothing about trading, holds no state a
trading path reads, and is imported by no gate. The RiskEngine remains the
final veto, the OMS owns order state, and nothing here reaches either --
asserted by a test in `tests/test_safety_invariants.py`, not promised.

It is deliberately not a second policy engine. `verification.py` DELEGATES to
`app.portfolio.control` (L61) for action classification and
`app.portfolio.decision` (L60) for the layer hierarchy, because a second
implementation of either would eventually disagree with the first, and the one
that disagrees silently is the one nobody is reading.
"""

from app.safety.certification import (
    GATES,
    CertificationState,
    Gate,
    GateResult,
    certify,
)
from app.safety.envelope import (
    DEFAULT_ENVELOPE,
    EnvelopeBreach,
    SafetyEnvelope,
    within_envelope,
)
from app.safety.invariants import (
    INVARIANTS,
    Invariant,
    InvariantStatus,
    by_id,
    enforced,
    unenforced,
)
from app.safety.verification import (
    PolicyVerificationEngine,
    PolicyVerificationResult,
    VerificationDecision,
    Violation,
    fingerprint,
)

__all__ = [
    "DEFAULT_ENVELOPE",
    "GATES",
    "INVARIANTS",
    "CertificationState",
    "EnvelopeBreach",
    "Gate",
    "GateResult",
    "Invariant",
    "InvariantStatus",
    "PolicyVerificationEngine",
    "PolicyVerificationResult",
    "SafetyEnvelope",
    "VerificationDecision",
    "Violation",
    "by_id",
    "certify",
    "enforced",
    "fingerprint",
    "unenforced",
    "within_envelope",
]
