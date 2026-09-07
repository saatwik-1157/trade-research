"""What changed since the last view, and nothing else. Section 32.

**A figure that is the same as last time is not an event.** The same rule L29
applied to model health, for the same reason: a stream that republishes the
whole portfolio on a timer is a poll wearing a WebSocket, and a client that
receives one every second learns nothing from any of them.

**A threshold crossing is an event; a threshold being exceeded is not.** A
drawdown that has been past 10% for an hour raises one alert, not 3,600. The
comparison is against the PREVIOUS view, so the transition is what fires — and
the recovery fires too, because "it stopped" is as much a fact as "it started".

Pure and synchronous. This decides what to publish; `PortfolioService` does the
publishing, and the hub does the delivery. Nothing here touches a session, a
clock or a socket, so the interesting cases are unit-testable without any of
them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - types only
    from app.portfolio.service import PortfolioView

#: Fractions of peak equity at which a drawdown is worth saying out loud.
#: Ladder rather than a single line so a deepening drawdown keeps reporting,
#: and round numbers because nothing measured picks them -- they are a display
#: convention, and the risk engine's own limits are the ones that bind.
DRAWDOWN_STEPS: tuple[Decimal, ...] = (
    Decimal("0.05"),
    Decimal("0.10"),
    Decimal("0.20"),
)


def _step(fraction: Decimal | None) -> Decimal | None:
    """The deepest ladder step this drawdown has passed, or None."""
    if fraction is None:
        return None
    passed = [s for s in DRAWDOWN_STEPS if fraction >= s]
    return max(passed) if passed else None


def changes_between(
    previous: PortfolioView | None, current: PortfolioView
) -> list[tuple[str, dict[str, Any]]]:
    """The events one refresh implies, as `(EventType value, payload)`.

    `previous` is None on the first view of a process: everything is reported
    once, because a client that connected has seen nothing, and then only
    changes follow.

    **No payload carries a credential.** §55. The fields here are money,
    counts and states; the account IDENTIFIER is included because a subscriber
    is already on that account's channel, and the broker login, server and
    password are not in `AccountState` at all -- `from_broker_account` never
    copies them, so there is no field to leak.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    account = current.account

    money = {
        "account_id": account.account_id,
        "environment": account.environment,
        "currency": account.currency,
        "balance": _s(account.balance),
        "equity": _s(account.equity),
        "unrealized_pnl": _s(current.pnl.unrealized if current.pnl else None),
        "realized_today": _s(current.pnl.realized_today if current.pnl else None),
        "margin_used": _s(account.margin_used),
        "freshness": str(account.freshness),
        "as_of": account.as_of.isoformat() if account.as_of else None,
    }
    if previous is None or _money_of(previous) != _money_of(current):
        out.append(("PORTFOLIO_UPDATED", money))

    if current.exposure is not None:
        gross, net = current.exposure.total.gross, current.exposure.total.net
        before = previous.exposure if previous else None
        if before is None or (before.total.gross, before.total.net) != (gross, net):
            out.append(
                (
                    "EXPOSURE_UPDATED",
                    {
                        "account_id": account.account_id,
                        "environment": account.environment,
                        "gross": str(gross),
                        "net": str(net),
                        "positions": current.exposure.total.positions,
                        "uncomputable": current.exposure.total.uncomputable,
                        "note": (
                            "gross is total absolute exposure and net is the directional "
                            "difference. They are different numbers and are never conflated."
                        ),
                    },
                )
            )

    now_step = _step(current.drawdown.current_pct if current.drawdown else None)
    was_step = _step(previous.drawdown.current_pct if previous and previous.drawdown else None)
    if now_step != was_step:
        out.append(
            (
                "DRAWDOWN_ALERT",
                {
                    "account_id": account.account_id,
                    "environment": account.environment,
                    "crossed": str(now_step) if now_step is not None else None,
                    "previous": str(was_step) if was_step is not None else None,
                    "direction": "deepened" if _deeper(now_step, was_step) else "recovered",
                    "current_pct": (
                        float(current.drawdown.current_pct)
                        if current.drawdown and current.drawdown.current_pct is not None
                        else None
                    ),
                    "peak_equity": _s(current.drawdown.peak_equity if current.drawdown else None),
                    "authority": (
                        "reported, not enforced. The RISK ENGINE decides whether a "
                        "drawdown permits a new trade; this says only that a display "
                        "threshold was crossed."
                    ),
                },
            )
        )

    if previous is None or previous.health is not current.health:
        out.append(
            (
                "PORTFOLIO_HEALTH_CHANGED",
                {
                    "account_id": account.account_id,
                    "environment": account.environment,
                    "health": str(current.health),
                    "previous": str(previous.health) if previous else None,
                    "reasons": current.health_reasons,
                },
            )
        )
    return out


def _deeper(now: Decimal | None, was: Decimal | None) -> bool:
    if now is None:
        return False
    return was is None or now > was


def _money_of(view: PortfolioView) -> tuple[Any, ...]:
    """The figures whose movement is worth an event.

    Freshness is in the tuple: a view going stale changes nothing about the
    numbers and changes everything about how they should be read, so it is a
    change a subscriber must see.
    """
    return (
        view.account.balance,
        view.account.equity,
        view.account.margin_used,
        view.pnl.unrealized if view.pnl else None,
        view.pnl.realized_today if view.pnl else None,
        view.account.freshness,
    )


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


__all__ = ["DRAWDOWN_STEPS", "changes_between"]
