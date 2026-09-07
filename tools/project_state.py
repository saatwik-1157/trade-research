#!/usr/bin/env python3
"""Generate PROJECT_STATE.json by measuring, not by remembering.

    python tools/project_state.py                 # print, do not write
    python tools/project_state.py --write         # rewrite PROJECT_STATE.json

**Why this exists.** `PROJECT_STATE.json` was hand-written and carried
`"generated_by": "hand"`. By 2026-09-06 it had drifted from the database on six
counts at once -- it reported 0 datasets against 1 READY dataset of 638 rows, 0
training runs against 3, 0 models against 1, and repeated the claim that
`market_bars` is empty when the table held 705 bars across 2 symbols.

None of those drifts was careless. Each was true when written. That is the
point: **a hand-written state file is a photograph presented as a window**, and
the only durable fix is to take the photograph on demand.

**The one rule this tool obeys, from CLAUDE.md:** a figure that could not be
measured is `null` and is listed in `unmeasured`. It is never written as 0.
Zero is a measurement -- "there are no datasets" -- and reporting an
unreachable database as zero would be exactly the fabrication the rest of the
platform refuses.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE = ROOT / "PROJECT_STATE.json"

#: Each figure and the query that establishes it. Kept together so the file can
#: name its own provenance rather than asserting numbers.
COUNTS: dict[str, str] = {
    "market_bars": "select count(*) from market_bars",
    "market_bar_symbols": "select count(distinct provider_symbol) from market_bars",
    "datasets": "select count(*) from datasets",
    "datasets_ready": "select count(*) from datasets where status = 'READY'",
    "model_versions": "select count(*) from model_versions",
    "training_runs": "select count(*) from training_runs",
    "strategies": "select count(*) from strategies",
    "strategy_versions": "select count(*) from strategy_versions",
    "bots": "select count(*) from bots",
    "orders": "select count(*) from orders",
    "trades": "select count(*) from trades",
    "positions_open": "select count(*) from positions where status = 'open'",
    "capital_reservations": "select count(*) from capital_reservations",
    "signals": "select count(*) from signals",
    "accounts_paper": "select count(*) from paper_accounts",
    "accounts_broker": "select count(*) from broker_accounts",
}


def psql(sql: str, *, container: str = "tr-postgres", user: str = "trade") -> str | None:
    """One scalar from the running database, or None if it could not be read.

    None rather than 0 on every failure path. The distinction is the whole
    point of the tool.
    """
    try:
        out = subprocess.run(
            ["docker", "exec", container, "psql", "-U", user, "-d", user, "-tAc", sql],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def health() -> dict[str, Any]:
    """The running API's own account of itself, or an explicit absence."""
    try:
        import urllib.error
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=10) as r:
            return dict(json.loads(r.read().decode()))
    except Exception:
        return {}


def schema_head() -> str | None:
    return psql("select version_num from alembic_version")


def repo_counts() -> dict[str, int]:
    """Things measurable from the tree rather than the database."""
    return {
        "backend_modules": len(list((ROOT / "backend" / "app").rglob("*.py"))),
        "backend_test_files": len(list((ROOT / "backend" / "tests").glob("test_*.py"))),
        "frontend_sources": len(
            [p for p in (ROOT / "frontend" / "src").rglob("*.ts*") if p.is_file()]
        ),
        "documents": len(list(ROOT.glob("*.md"))),
        "migrations": len(list((ROOT / "backend" / "alembic" / "versions").glob("*.py"))),
    }


def invariants() -> dict[str, Any]:
    """The safety registry's own tally, imported rather than transcribed."""
    sys.path.insert(0, str(ROOT / "backend"))
    try:
        from app.safety.certification import certify  # noqa: PLC0415
        from app.safety.invariants import INVARIANTS, InvariantStatus  # noqa: PLC0415
    except Exception:
        return {"available": False, "why": "the safety package could not be imported"}
    cert = certify(now=datetime.now(timezone.utc))
    return {
        "available": True,
        "total": len(INVARIANTS),
        "by_status": {
            s.value: sum(1 for i in INVARIANTS if i.status is s) for s in InvariantStatus
        },
        "certification": cert.state.value,
        "gates_not_passing": [
            g.gate.id for g in cert.gates if g.status.value != "PASS"
        ],
    }


def build() -> dict[str, Any]:
    measured: dict[str, int] = {}
    unmeasured: list[str] = []
    for name, sql in COUNTS.items():
        raw = psql(sql)
        if raw is None or not raw.lstrip("-").isdigit():
            unmeasured.append(name)
            continue
        measured[name] = int(raw)

    api = health()
    head = schema_head()

    return {
        "$comment": [
            "GENERATED by tools/project_state.py. Do not hand-edit: the previous",
            "hand-written version drifted from the database on six counts at once,",
            "each of which was true when written.",
            "A figure that could not be measured is absent from `measured` and named",
            "in `unmeasured`. It is NEVER written as 0 -- zero is a measurement.",
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "tools/project_state.py",
        "measurement_source": "the running deployment: tr-postgres and /api/health",
        "trading_mode": api.get("trading_mode"),
        "live_trading": api.get("live_trading"),
        "live_execution_allowed": api.get("live_execution_allowed"),
        "live_blockers": (
            len(api["live_execution_blockers"])
            if isinstance(api.get("live_execution_blockers"), list)
            else None
        ),
        "api_reachable": bool(api),
        "schema_head": head,
        "measured": measured,
        "unmeasured": unmeasured,
        "provenance": {k: v for k, v in COUNTS.items() if k in measured},
        "repo": repo_counts(),
        "safety": invariants(),
        "notes": {
            "market_data": (
                "Correlation, fragility and cross-instrument stress need a common "
                "window across TWO OR MORE instruments. Report the symbol count, "
                "never 'the table is empty' -- that phrasing goes stale on the first "
                "ingestion and did."
            ),
            "authority": (
                "A measurement of the deployment, not a claim that anything is "
                "correct. The RiskEngine remains the final veto."
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite PROJECT_STATE.json")
    args = parser.parse_args()

    state = build()
    text = json.dumps(state, indent=2, sort_keys=False) + "\n"

    if args.write:
        STATE.write_text(text, encoding="utf-8")
        print(f"wrote {STATE.relative_to(ROOT)}")
    else:
        print(text)

    if state["unmeasured"]:
        print(
            f"\n{len(state['unmeasured'])} figure(s) could not be measured and are "
            f"reported as absent rather than zero: {', '.join(state['unmeasured'])}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
