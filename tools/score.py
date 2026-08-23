"""Deterministic composite scoring.

Every subscore is a pure function of numbers that were computed from real data,
and every subscore returns the components that produced it, so any figure in a
report can be traced back to the arithmetic that made it.

Two rules this module exists to enforce:

1. A component with no data contributes nothing. It is dropped and the
   remaining weights are renormalised, and the omission is reported. Missing
   data never silently becomes a neutral 50.
2. The composite is a *descriptive* summary of measurable state, not a
   forecast. Whether it carries any forward-return signal is an empirical
   question answered by tools/backtest.py, not by this file.
"""
from __future__ import annotations

# Default weights. Deliberately excludes social/news sentiment: there is no way
# to score a headline on a 0-100 scale without inventing the number, so
# sentiment stays qualitative and unscored in the written report.
DEFAULT_WEIGHTS = {
    "technical": 0.30,
    "quality": 0.25,
    "valuation": 0.20,
    "risk": 0.15,
    "analyst": 0.10,
}


def _band(x, points):
    """Piecewise-linear map through (input, output) breakpoints, clamped at the ends."""
    if x is None:
        return None
    pts = sorted(points)
    if x <= pts[0][0]:
        return float(pts[0][1])
    if x >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            span = x1 - x0
            return float(y0 if span == 0 else y0 + (y1 - y0) * (x - x0) / span)
    return None


def _blend(parts: dict):
    """Average the non-None parts; returns (score, used, missing)."""
    used = {k: v for k, v in parts.items() if v is not None}
    missing = sorted(k for k, v in parts.items() if v is None)
    if not used:
        return None, {}, missing
    return round(sum(used.values()) / len(used), 2), used, missing


def technical_score(t: dict) -> dict:
    """Trend, momentum and trend quality. Pure function of computed indicators."""
    macd_hist = (t.get("macd") or {}).get("histogram")
    parts = {
        # Distance above/below the 200-day: the single most-studied trend filter
        "trend_200d": _band(t.get("price_vs_sma200"), [(-0.30, 5), (-0.10, 25), (0, 50), (0.10, 70), (0.35, 90)]),
        "trend_50d": _band(t.get("price_vs_sma50"), [(-0.20, 10), (-0.05, 35), (0, 50), (0.05, 65), (0.20, 88)]),
        # 12-1 momentum, the classic cross-sectional momentum definition
        "momentum_12_1": _band(t.get("momentum_12_1"), [(-0.50, 5), (-0.15, 30), (0, 50), (0.20, 70), (0.75, 92)]),
        # RSI scored as distance from neutral, penalising both extremes
        "rsi_position": _band(t.get("rsi14"), [(15, 20), (30, 45), (50, 65), (70, 45), (85, 15)]),
        "macd_histogram": None if macd_hist is None else (65.0 if macd_hist > 0 else 35.0),
        # ADX rewards a trend that is actually trending, in either direction
        "trend_quality": _band(t.get("adx14"), [(10, 35), (20, 50), (30, 68), (50, 80)]),
        "volume_confirmation": _band(t.get("obv_slope_norm"), [(-3, 25), (0, 50), (3, 75)]),
    }
    if t.get("golden_cross") is not None:
        parts["golden_cross"] = 62.0 if t["golden_cross"] else 38.0
    score, used, missing = _blend(parts)
    return {"score": score, "components": used, "missing": missing}


def risk_score(t: dict) -> dict:
    """Higher is safer. Volatility, drawdown, beta and tradeable liquidity."""
    dollar_vol = t.get("dollar_volume_20d")
    parts = {
        "realized_vol": _band(t.get("realized_vol_60d"), [(0.12, 90), (0.25, 70), (0.40, 48), (0.60, 28), (1.00, 8)]),
        "drawdown_1y": _band(t.get("max_drawdown_1y"), [(-0.70, 8), (-0.45, 28), (-0.25, 52), (-0.12, 75), (-0.05, 90)]),
        "beta": _band(t.get("beta_vs_benchmark"), [(0.4, 82), (0.8, 68), (1.0, 55), (1.5, 35), (2.5, 12)]),
        "atr_pct": _band(t.get("atr_pct"), [(0.008, 88), (0.02, 65), (0.04, 40), (0.08, 15)]),
        # Liquidity in log10 dollars/day: 1e6 is thin, 1e9 is institutional
        "liquidity": None if not dollar_vol or dollar_vol <= 0 else _band(
            __import__("math").log10(dollar_vol), [(5, 10), (6, 35), (7, 60), (8, 80), (9.5, 92)]
        ),
    }
    score, used, missing = _blend(parts)
    return {"score": score, "components": used, "missing": missing}


def quality_score(edgar_derived: dict | None, provider_fundamentals: dict | None) -> dict:
    """Business quality from filed financials, falling back to provider fields.

    EDGAR wins wherever both are present, because one is a filing and the other
    is a vendor's summary of it.
    """
    e = edgar_derived or {}
    p = provider_fundamentals or {}

    def prefer(edgar_key, provider_key):
        v = e.get(edgar_key)
        return v if v is not None else p.get(provider_key)

    parts = {
        "gross_margin": _band(prefer("gross_margin", "grossMargins"), [(0.05, 15), (0.25, 40), (0.45, 65), (0.70, 88)]),
        "operating_margin": _band(prefer("operating_margin", "operatingMargins"), [(-0.15, 8), (0, 35), (0.12, 58), (0.30, 82), (0.50, 92)]),
        "net_margin": _band(prefer("net_margin", "profitMargins"), [(-0.15, 8), (0, 35), (0.10, 58), (0.25, 82), (0.45, 93)]),
        "return_on_equity": _band(prefer("return_on_equity", "returnOnEquity"), [(-0.10, 8), (0.05, 35), (0.15, 60), (0.30, 82), (0.55, 93)]),
        "revenue_growth": _band(
            e.get("revenue_yoy") if e.get("revenue_yoy") is not None else p.get("revenueGrowth"),
            [(-0.25, 8), (0, 35), (0.10, 55), (0.30, 78), (0.70, 93)],
        ),
        "fcf_margin": _band(e.get("fcf_margin"), [(-0.15, 8), (0, 38), (0.10, 60), (0.25, 84), (0.40, 93)]),
        # Leverage: EDGAR reports a ratio, the provider reports a percentage
        "leverage": _band(
            e.get("debt_to_equity") if e.get("debt_to_equity") is not None
            else (p.get("debtToEquity") / 100 if p.get("debtToEquity") is not None else None),
            [(0, 88), (0.3, 72), (0.8, 52), (1.8, 28), (4.0, 8)],
        ),
    }
    score, used, missing = _blend(parts)
    return {"score": score, "components": used, "missing": missing}


def _meaningful_multiple(x):
    """A multiple is only interpretable when its denominator is positive.

    A loss-making company has a negative P/E and a negative EV/EBITDA. Passing
    those through a "lower is cheaper" band clamps them to the cheap end and
    scores a cash-burning business as a bargain - which is not a caveat, it is
    a wrong answer. Undefined multiples are dropped so the component is scored
    on whatever remains, and the omission is reported.
    """
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def valuation_score(valuation: dict | None, quality_hint: float | None = None) -> dict:
    """Cheaper scores higher. Absolute multiples only - no sector normalisation.

    Without a sector comparison set these bands treat a low multiple as cheap,
    which systematically flatters value traps and penalises fast compounders.
    That limitation is reported rather than papered over.
    """
    v = valuation or {}
    parts = {
        "forward_pe": _band(_meaningful_multiple(v.get("forwardPE")), [(5, 88), (12, 72), (20, 55), (35, 32), (70, 10)]),
        "trailing_pe": _band(_meaningful_multiple(v.get("trailingPE")), [(5, 85), (15, 68), (25, 50), (45, 28), (90, 8)]),
        "price_to_sales": _band(_meaningful_multiple(v.get("priceToSalesTrailing12Months")), [(0.5, 88), (2, 68), (5, 48), (12, 22), (25, 6)]),
        "ev_to_ebitda": _band(_meaningful_multiple(v.get("enterpriseToEbitda")), [(4, 88), (10, 68), (18, 46), (30, 22), (60, 6)]),
        "price_to_book": _band(_meaningful_multiple(v.get("priceToBook")), [(0.7, 85), (2, 66), (5, 46), (12, 22), (30, 6)]),
    }
    negative = sorted(
        k for k, src in (
            ("forward_pe", v.get("forwardPE")),
            ("trailing_pe", v.get("trailingPE")),
            ("ev_to_ebitda", v.get("enterpriseToEbitda")),
            ("price_to_book", v.get("priceToBook")),
            ("price_to_sales", v.get("priceToSalesTrailing12Months")),
        )
        if src is not None and _meaningful_multiple(src) is None
    )
    score, used, missing = _blend(parts)
    out = {
        "score": score,
        "components": used,
        "missing": missing,
        "caveat": "Absolute multiples, not sector-relative. A low score on a high-growth "
                  "name and a high score on a declining one are both expected artefacts.",
    }
    if negative:
        out["undefined_multiples"] = negative
        out["undefined_note"] = (
            "Dropped as undefined because the denominator is negative (the company is "
            "loss-making or has negative book value). Valuation here is scored only on "
            "the remaining multiples and is correspondingly less informative."
        )
    return out


def analyst_score(analyst: dict | None, price: float | None) -> dict:
    """Sell-side consensus. Structured vendor data, not scraped text."""
    a = analyst or {}
    target = a.get("targetMeanPrice")
    upside = (target / price - 1) if (target and price) else None
    parts = {
        # recommendationMean: 1.0 = strong buy, 5.0 = sell
        "consensus_rating": _band(a.get("recommendationMean"), [(1.0, 90), (2.0, 68), (3.0, 45), (4.0, 25), (5.0, 10)]),
        "target_upside": _band(upside, [(-0.30, 10), (-0.05, 38), (0.10, 58), (0.30, 78), (0.75, 90)]),
        "coverage_breadth": _band(a.get("numberOfAnalystOpinions"), [(1, 30), (5, 48), (15, 62), (40, 72)]),
    }
    score, used, missing = _blend(parts)
    return {
        "score": score,
        "components": used,
        "missing": missing,
        "target_upside": round(upside, 6) if upside is not None else None,
        "caveat": "Sell-side targets are known to be optimistic on average and to lag price. "
                  "Treated as one input, capped at 10% of the composite.",
    }


def composite(subscores: dict, weights: dict | None = None) -> dict:
    """Weighted blend over available components, with renormalisation reported."""
    w = dict(weights or DEFAULT_WEIGHTS)
    available = {k: subscores[k]["score"] for k in w if subscores.get(k, {}).get("score") is not None}
    dropped = sorted(k for k in w if k not in available)

    if not available:
        return {
            "score": None,
            "reason": "no component could be computed from available data",
            "dropped_components": dropped,
        }

    total_w = sum(w[k] for k in available)
    effective = {k: round(w[k] / total_w, 4) for k in available}
    value = sum(available[k] * effective[k] for k in available)

    return {
        "score": round(value, 1),
        "component_scores": {k: round(v, 2) for k, v in available.items()},
        "nominal_weights": {k: w[k] for k in available},
        "effective_weights": effective,
        "dropped_components": dropped,
        "coverage": round(total_w / sum(w.values()), 3),
        "interpretation": (
            "A descriptive 0-100 summary of measured trend, filed financials, multiples, "
            "volatility and sell-side consensus as of the data date. It is not a price "
            "forecast and not a recommendation. Run tools/backtest.py to measure whether "
            "this composite has separated forward returns historically."
        ),
    }


def score_all(tech: dict, edgar: dict | None, profile: dict | None, weights: dict | None = None) -> dict:
    """Full scoring pass over one snapshot."""
    profile = profile or {}
    edgar_derived = (edgar or {}).get("derived") if (edgar or {}).get("available") else None

    subs = {
        "technical": technical_score(tech),
        "risk": risk_score(tech),
        "quality": quality_score(edgar_derived, profile.get("fundamentals")),
        "valuation": valuation_score(profile.get("valuation")),
        "analyst": analyst_score(profile.get("analyst"), tech.get("price")),
    }
    return {"subscores": subs, "composite": composite(subs, weights)}
