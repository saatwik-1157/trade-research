"""Security controls added at L39, on top of the ones L04 and L06 already had.

This package deliberately contains **no authentication, no authorization and no
session handling**. Those live in `app/auth` and have since L04, and a second
implementation of either would be the duplicate system the brief forbids. What
is here is what the audit found genuinely missing:

    headers.py   response security headers and the CORS rule that is enforced
                 rather than documented
    stepup.py    re-authentication for the actions a stolen cookie must not
                 reach -- the gap L36 and L38 both named
    events.py    the SECURITY event vocabulary, filling L34's empty
                 `Category.security`
    posture.py   the security status, reported as one component of L37's
                 existing health stack rather than as a second dashboard

Nothing in this package can place, cancel or modify an order, and nothing in it
can grant a permission. Both are asserted by a test that walks the imports.
"""

from app.security.events import SecurityEvent, SecurityRecord, SecuritySeverity, record
from app.security.headers import BASE_HEADERS, SecurityHeadersMiddleware
from app.security.posture import component, scorecard
from app.security.stepup import StepUp, StepUpError, StepUpScope

__all__ = [
    "BASE_HEADERS",
    "SecurityEvent",
    "SecurityRecord",
    "SecuritySeverity",
    "StepUp",
    "StepUpError",
    "StepUpScope",
    "component",
    "SecurityHeadersMiddleware",
    "record",
    "scorecard",
]
