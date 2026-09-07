"""Shared route dependencies: idempotency keys and correlation.

The idempotency key is prepared here and consumed at L19. That split is
deliberate: the *contract* a caller must satisfy has to be stable before the
OMS exists, because the callers are written against it first. What this
module does today is validate the key's shape and hand it to the route; what
it does not do is pretend a key has been recorded, because no write path
exists to record it against.

`orders.intent_id` is the column the key eventually lands in. It is unique,
which is what makes a replayed submission a conflict rather than a second
order.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import Depends, Header, Request

from app.core.errors import ValidationFailed

IDEMPOTENCY_HEADER = "Idempotency-Key"

# Deliberately narrow: the value becomes a database key and appears in logs,
# so it is restricted to characters that cannot confuse either. A UUID, a
# ULID and a broker ticket all fit; a newline or a quote does not.
_KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


def idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> str | None:
    """The caller's replay key, validated but not yet recorded.

    Absent is allowed at this level and refused at L19 for write paths that
    can move money. Present-but-malformed is refused now: accepting a key the
    OMS could not store would let a caller believe it had replay protection
    it does not have.
    """
    if idempotency_key is None:
        return None
    value = idempotency_key.strip()
    if not _KEY.match(value):
        raise ValidationFailed(
            f"{IDEMPOTENCY_HEADER} must be 8-128 characters of letters, digits, "
            "'_', '-', '.' or ':'"
        )
    return value


IdempotencyKey = Annotated[str | None, Depends(idempotency_key)]


def correlation_id(request: Request) -> str:
    """The request id minted by `app.core.errors.request_id_middleware`.

    Services take it as an argument rather than reaching for a context
    variable, so a worker running the same service outside a request can pass
    its own and the trace stays continuous.
    """
    from app.core.errors import request_id_of

    return request_id_of(request)


CorrelationId = Annotated[str, Depends(correlation_id)]
