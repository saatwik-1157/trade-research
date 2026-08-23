"""Build the numeric snapshot for a ticker.

This is the contract between the data layer and the agents. Everything an agent
is allowed to state as fact lives in the JSON this produces; anything absent
here has to be either sourced with a URL by the agent or left out of the report.

Usage:
    python tools/snapshot.py NVDA
    python tools/snapshot.py NVDA --quick
    python tools/snapshot.py NVDA --out reports/NVDA.snapshot.json
    python tools/snapshot.py NVDA --asof 2026-03-31   # point-in-time technicals
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402

import edgar  # noqa: E402
import indicators  # noqa: E402
import market  # noqa: E402
import score as scoring  # noqa: E402


def build(ticker: str, period: str = "3y", quick: bool = False, asof: str | None = None) -> dict:
    ticker = ticker.upper().strip()
    df = market.get_ohlcv(ticker, period=period)

    if asof:
        cutoff = pd.Timestamp(asof)
        df = df[df.index <= cutoff]
        if len(df) < 60:
            raise ValueError(f"Only {len(df)} bars available for {ticker} on or before {asof}")

    bench = None if (quick or asof) else market.get_benchmark_close(period=period)
    tech = indicators.compute_all(df, bench)

    snap: dict = {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "quick" if quick else "full",
        "technical": tech,
        "provenance": market.provenance(ticker, df),
    }

    if asof:
        snap["asof_cutoff"] = asof
        snap["point_in_time"] = {
            "technical": True,
            "fundamentals": False,
            "note": "Only price-derived fields are point-in-time. Fundamentals and analyst "
                    "data reflect today and must not be used in a historical study.",
        }
        snap["scores"] = {
            "subscores": {
                "technical": scoring.technical_score(tech),
                "risk": scoring.risk_score(tech),
            }
        }
        snap["scores"]["composite"] = scoring.composite(
            snap["scores"]["subscores"], weights={"technical": 0.67, "risk": 0.33}
        )
        return snap

    profile = market.get_profile(ticker)
    snap["profile"] = profile.get("profile", {})
    snap["valuation"] = profile.get("valuation", {})
    snap["provider_fundamentals"] = profile.get("fundamentals", {})
    snap["analyst"] = profile.get("analyst", {})
    snap["shares"] = profile.get("shares", {})
    snap["provenance"]["profile_data"] = {
        "source": market.PROVIDER,
        "authoritative": False,
        "note": "Vendor summary fields. SEC EDGAR overrides these wherever both exist.",
        "unavailable_fields": profile.get("missing_fields", []),
    }

    if not quick:
        try:
            fundamentals = edgar.fundamentals(ticker)
        except Exception as exc:
            fundamentals = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        snap["edgar"] = fundamentals
        snap["earnings_dates"] = market.get_earnings_dates(ticker)
        snap["provenance"]["fundamentals"] = fundamentals.get("provenance", {"source": "unavailable"})

    snap["scores"] = scoring.score_all(tech, snap.get("edgar"), profile)

    # An explicit inventory of what could not be resolved. An agent that reads
    # this knows exactly which claims it is not entitled to make.
    gaps = []
    if not (snap.get("edgar") or {}).get("available") and not quick:
        gaps.append(f"SEC filed fundamentals unavailable: {(snap.get('edgar') or {}).get('reason', 'unknown')}")
    for key in ("technical",):
        gaps += [f"technical.{k} could not be computed" for k, v in snap[key].items() if v is None]
    for name, sub in snap["scores"]["subscores"].items():
        for miss in sub.get("missing", []):
            gaps.append(f"score.{name}.{miss} had no input data")
    snap["data_gaps"] = gaps

    return snap


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a fully sourced numeric snapshot for a ticker.")
    ap.add_argument("ticker")
    ap.add_argument("--period", default="3y", help="history window to pull (default 3y)")
    ap.add_argument("--quick", action="store_true", help="skip SEC filings and benchmark beta")
    ap.add_argument("--asof", help="truncate price history to this date (point-in-time technicals only)")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args()

    try:
        snap = build(args.ticker, period=args.period, quick=args.quick, asof=args.asof)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}", "ticker": args.ticker}, indent=2))
        return 1

    payload = json.dumps(snap, indent=2, default=str)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        composite = snap["scores"]["composite"]
        print(f"Wrote {args.out}")
        print(f"  {snap['ticker']}  price={snap['technical']['price']}  asof={snap['technical']['asof']}")
        print(f"  composite={composite.get('score')}  coverage={composite.get('coverage')}")
        if snap.get("data_gaps"):
            print(f"  data gaps: {len(snap['data_gaps'])} (see data_gaps in the JSON)")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
