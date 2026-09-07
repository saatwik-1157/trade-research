"""The live preflight command.

    python -m app.live.preflight              # human-readable
    python -m app.live.preflight --json       # machine-readable
    python -m app.live.preflight --strict     # exit 1 unless READY_FOR_LIVE

It prints every check the `LiveTradingGate` runs and ends with one word:
`READY_FOR_LIVE` or `NOT_READY_FOR_LIVE`.

**It places nothing, changes nothing and enables nothing.** It reads settings,
probes the database and Redis with `app.core.health`'s own checks rather than
new ones, and reads the connected MetaTrader account through the read-only
`tools/mt5_account.py`. There is no code path from this module to an order.

**What it cannot see, it fails.** A CLI is a different process from the API, so
the in-process registries -- the broker adapter registry, the OMS, the risk
service, the safe-mode latch -- are not reachable from here, and every check
that depends on one reports FAIL with "nothing was supplied to check". That is
the correct answer rather than a limitation to apologise for: this command
answers "is this deployment ready for live trading", and a deployment whose
execution machinery cannot be observed is not ready. An in-process caller that
holds those objects can build a fuller `LiveContext` and get a fuller answer
from the same gate.

Exit codes: 0 when the report was produced, 1 with `--strict` when it is not
READY_FOR_LIVE, 2 when the command itself failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.core.health import check_database, check_redis
from app.core.settings import get_settings
from app.live.allowlist import Allowlist
from app.live.gate import (
    ConnectedAccount,
    GateReport,
    LiveContext,
    LiveTradingGate,
    Verdict,
)

#: Where the read-only MT5 helpers live, relative to the repository root. The
#: preflight imports the SAME module the trading toolkit uses rather than
#: opening a terminal of its own -- a second connect() is how four of them
#: appeared in this repository before L10 removed three.
_TOOLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "tools",
)

_TRADE_MODE = {0: "DEMO", 1: "CONTEST", 2: "REAL"}


def _dec(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def read_connected_account() -> tuple[ConnectedAccount | None, str]:
    """Read the account the terminal is logged in to. Read-only, always.

    Returns `(account, detail)`. `None` with a reason is a normal outcome: the
    API image is Linux and has no MetaTrader5 package, and a terminal that is
    not running is exactly the condition this check must report rather than
    guess around.
    """
    if _TOOLS not in sys.path:
        sys.path.insert(0, _TOOLS)
    try:
        import MetaTrader5 as mt5  # noqa: N813
    except ImportError:
        return None, "the MetaTrader5 package is not installed in this environment"

    try:
        if not mt5.initialize():
            return None, f"terminal did not initialise: {mt5.last_error()}"
    except Exception as exc:  # noqa: BLE001 - a terminal can fail in many ways
        return None, f"terminal initialise raised {type(exc).__name__}: {exc}"

    try:
        info = mt5.account_info()
        if info is None:
            return None, "no account is logged in to the terminal"
        terminal = mt5.terminal_info()
        mode = _TRADE_MODE.get(info.trade_mode, f"UNKNOWN({info.trade_mode})")
        return (
            ConnectedAccount(
                account_id=str(info.login),
                server=str(getattr(info, "server", "")),
                broker=str(getattr(info, "company", "")),
                account_type=mode,
                currency=str(getattr(info, "currency", "")),
                balance=_dec(getattr(info, "balance", None)),
                equity=_dec(getattr(info, "equity", None)),
                free_margin=_dec(getattr(info, "margin_free", None)),
                leverage=int(getattr(info, "leverage", 0)) or None,
                trade_allowed=bool(getattr(terminal, "trade_allowed", False)),
            ),
            "read from the terminal",
        )
    finally:
        mt5.shutdown()


async def _infrastructure(settings: object) -> dict[str, bool | None]:
    """Database and Redis, using the platform's own probes."""
    timeout = float(getattr(settings, "health_timeout_seconds", 2.0))
    try:
        db = await check_database(str(getattr(settings, "database_url", "")), timeout)
        redis = await check_redis(str(getattr(settings, "redis_url", "")), timeout)
    except Exception:  # noqa: BLE001 - an unreachable probe is a failed check
        return {"database": None, "redis": None}
    return {"database": db.ok, "redis": redis.ok}


def build_context(*, account_override: ConnectedAccount | None = None) -> tuple[LiveContext, str]:
    """Assemble what this process can actually observe."""
    settings = get_settings()
    account: ConnectedAccount | None
    if account_override is not None:
        account, detail = account_override, "supplied by the caller"
    else:
        account, detail = read_connected_account()
    infra = asyncio.run(_infrastructure(settings))

    ctx = LiveContext(
        settings=settings,
        now=datetime.now(UTC),
        allowlist=Allowlist.of(getattr(settings, "live_allowed_accounts", ())),
        expected_account_id=getattr(settings, "live_expected_account_id", "") or None,
        expected_server=getattr(settings, "live_expected_server", "") or None,
        connected_account=account,
        terminal_reachable=account is not None,
        market_data_max_age_seconds=float(getattr(settings, "live_quote_max_age_seconds", 60.0)),
        step_up_required=bool(getattr(settings, "step_up_required", False)),
        database_healthy=infra["database"],
        redis_healthy=infra["redis"],
        # Deliberately left unset: these live inside the API process and this
        # is not it. Unset fails, which is the honest answer from here.
        adapter_registered=None,
        adapter_mode=None,
        adapter_healthy=None,
        risk_engine_healthy=None,
        risk_limits_configured=None,
        capital=None,
        kill_switches_available=None,
        position_sizer_healthy=None,
        oms_healthy=None,
        position_manager_healthy=None,
        unresolved_orders=None,
        unknown_orders=None,
        positions_reconciled=None,
        orders_reconciled=None,
        unexpected_positions=None,
        safe_mode_engaged=None,
        recovery_available=None,
        monitoring_active=None,
        workers_healthy=None,
        audit_logging_active=None,
        notifications_healthy=None,
        release_stamped=None,
    )
    return ctx, detail


_MARK = {Verdict.passed: "PASS", Verdict.failed: "FAIL", Verdict.warning: "WARN"}


def render(report: GateReport, account_detail: str) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append("  LIVE TRADING PREFLIGHT")
    lines.append(f"  generated {report.generated_at:%Y-%m-%d %H:%M:%S %Z}")
    lines.append(f"  connected account: {account_detail}")
    lines.append("")

    group = None
    for check in report.checks:
        if check.group != group:
            group = check.group
            lines.append(f"  [{group}]")
        flag = _MARK[check.verdict]
        optional = "" if check.mandatory else "  (non-blocking)"
        lines.append(f"    {flag}  {check.name}{optional}")
        lines.append(f"          {check.detail}")
    lines.append("")

    counts = report.counts()
    lines.append(
        f"  {counts['passed']} passed, {counts['failed']} failed, "
        f"{counts['warning']} warning, {counts['blocking']} blocking"
    )
    lines.append("")
    if report.blockers:
        lines.append("  BLOCKERS")
        for check in report.blockers:
            lines.append(f"    - {check.name}: {check.detail}")
        lines.append("")
    lines.append(f"  {report.verdict}")
    lines.append("")
    if not report.ready:
        lines.append(
            "  Nothing was changed and nothing was armed. A blocker is fixed by meeting it,"
        )
        lines.append("  never by removing the check.")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the live-trading gate and report READY_FOR_LIVE or not.",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 unless the verdict is READY_FOR_LIVE",
    )
    args = ap.parse_args(argv)

    try:
        ctx, detail = build_context()
        report = LiveTradingGate().evaluate(ctx)
    except Exception as exc:  # noqa: BLE001 - a preflight that crashes is NOT ready
        message = f"preflight failed to run: {type(exc).__name__}: {exc}"
        if args.json:
            print(json.dumps({"verdict": "NOT_READY_FOR_LIVE", "error": message}, indent=2))
        else:
            print(f"\n  {message}\n\n  NOT_READY_FOR_LIVE\n")
        return 2

    if args.json:
        payload = report.as_dict()
        payload["connected_account"] = (
            ctx.connected_account.as_dict() if ctx.connected_account else None
        )
        payload["account_detail"] = detail
        print(json.dumps(payload, indent=2))
    else:
        print(render(report, detail))

    if args.strict and not report.ready:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
